# Turns every Windows Start-menu app into a GNOME launcher (name + Windows' own icon).
# Output, read by GNOME through XDG_DATA_DIRS (see gnome-nested.sh) and by winbridge.py:
#   share/applications/windows-<slug>.desktop   launches via win-launch.sh <base64 AppID> -> explorer.exe shell:AppsFolder\<id>
#   share/icons/windows/<slug>.png              256 px icon from the Windows shell
#   share/windows-apps.json                     id/name/AppID/exe, so mirrored windows get their app's icon
# Re-run after installing apps, then restart GNOME.
$ErrorActionPreference = "Stop"
$share = Join-Path $PSScriptRoot "share"
$appsDir = Join-Path $share "applications"
$iconDir = Join-Path $share "icons\windows"
$wslDir = wsl -d Ubuntu-24.04 --exec wslpath -a $PSScriptRoot
$launcher = "$wslDir/win-launch.sh"
$linuxIcons = "$wslDir/share/icons/windows"

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System; using System.Drawing; using System.Drawing.Imaging; using System.Runtime.InteropServices;
public static class ShellIcon {
    [StructLayout(LayoutKind.Sequential)] struct SIZE { public int cx, cy; }
    [StructLayout(LayoutKind.Sequential)] struct BITMAP { public int bmType, bmWidth, bmHeight, bmWidthBytes; public short bmPlanes, bmBitsPixel; public IntPtr bmBits; }
    [ComImport, Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItemImageFactory { [PreserveSig] int GetImage(SIZE size, int flags, out IntPtr phbm); }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    static extern int SHCreateItemFromParsingName(string path, IntPtr pbc, ref Guid riid, [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory item);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr h);
    [DllImport("gdi32.dll")] static extern int GetObject(IntPtr h, int size, ref BITMAP bm);

    // Icon of a shell item (e.g. shell:AppsFolder\<AppID>) as PNG, keeping its alpha channel.
    public static bool Save(string parsingName, string png, int size) {
        Guid iid = typeof(IShellItemImageFactory).GUID;
        IShellItemImageFactory f;
        if (SHCreateItemFromParsingName(parsingName, IntPtr.Zero, ref iid, out f) != 0) return false;
        IntPtr hbm;
        if (f.GetImage(new SIZE { cx = size, cy = size }, 0x1 | 0x4, out hbm) != 0) return false; // BIGGERSIZEOK | ICONONLY
        try {
            BITMAP bm = new BITMAP();
            GetObject(hbm, Marshal.SizeOf(bm), ref bm);
            if (bm.bmBits == IntPtr.Zero || bm.bmBitsPixel != 32) {
                using (Bitmap b = Image.FromHbitmap(hbm)) b.Save(png, ImageFormat.Png);
                return true;
            }
            using (Bitmap raw = new Bitmap(bm.bmWidth, bm.bmHeight, bm.bmWidthBytes, PixelFormat.Format32bppPArgb, bm.bmBits))
            using (Bitmap copy = new Bitmap(raw)) {
                copy.RotateFlip(RotateFlipType.RotateNoneFlipY); // DIB sections are stored bottom-up
                copy.Save(png, ImageFormat.Png);
            }
            return true;
        } finally { DeleteObject(hbm); }
    }
}
'@

# Not apps: Linux apps WSLg already exports, links to web pages and documents, uninstallers, docs.
function Skip($item, $target) {
    $name, $id = $item.Name, $item.Path
    if ($name -match '\(Ubuntu-[\d.]+\)$') { return $true }
    if ($id -match '^https?://') { return $true }
    if ("$id $target" -match '\.(url|html?|txt|ini|chm|pdf|md|rtf|log|xml)(\s|$)') { return $true }
    return $name -match '(?i)uninstall|readme|read me|documentation|dokumentation|release notes|\bfaq\b|website|manual|licen[cs]e'
}

$utf8 = New-Object System.Text.UTF8Encoding $false   # GLib's key-file parser chokes on a BOM
New-Item -ItemType Directory -Force $appsDir, $iconDir | Out-Null
Get-ChildItem $appsDir -Filter "windows-*.desktop" | Remove-Item
Get-ChildItem $iconDir -Filter "*.png" | Remove-Item

$md5 = [System.Security.Cryptography.MD5]::Create()
$index = @()
$folder = (New-Object -ComObject Shell.Application).NameSpace('shell:AppsFolder')
foreach ($item in $folder.Items()) {
    $target = $item.ExtendedProperty('System.Link.TargetParsingPath')
    if (Skip $item $target) { continue }
    $hash = -join ($md5.ComputeHash([Text.Encoding]::UTF8.GetBytes($item.Path))[0..2] | ForEach-Object { $_.ToString("x2") })
    $slug = (($item.Name.ToLower() -replace '[^a-z0-9]+', '-').Trim('-')) + "-" + $hash
    $id = "windows-$slug"
    $hasIcon = [ShellIcon]::Save("shell:AppsFolder\$($item.Path)", (Join-Path $iconDir "$slug.png"), 256)
    $desktop = @(
        "[Desktop Entry]"
        "Type=Application"
        "Name=$($item.Name)"
        "Comment=Windows"
        # AppID as base64: no Exec quoting rules for its backslashes/braces (%k comes out empty in GLib 2.80)
        "Exec=$launcher $([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($item.Path)))"
        $(if ($hasIcon) { "Icon=$linuxIcons/$slug.png" } else { "Icon=application-x-executable" })
        "Terminal=false"
        "StartupNotify=false"
        "X-Windows-AppID=$($item.Path)"
    ) -join "`n"
    [IO.File]::WriteAllText((Join-Path $appsDir "$id.desktop"), $desktop + "`n", $utf8)
    $index += [ordered]@{ id = $id; name = $item.Name; appid = $item.Path; exe = "$target".ToLower() }
}
[IO.File]::WriteAllText((Join-Path $share "windows-apps.json"), (ConvertTo-Json $index -Depth 3), $utf8)
"{0} Windows apps exported to {1}" -f $index.Count, $appsDir
