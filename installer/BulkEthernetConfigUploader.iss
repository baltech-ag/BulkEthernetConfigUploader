; Inno Setup script for BulkEthernetConfigUploader, built by "uv run inv installer".
; Per-user installation without administrator rights: %LOCALAPPDATA%\Programs\BulkEthernetConfigUploader
#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{7D4C2E9A-3B61-4F0D-9C58-2A6E8B1F5D37}
AppName=BulkEthernetConfigUploader
AppVersion={#Version}
AppPublisher=BALTECH AG
AppPublisherURL=https://www.baltech.de
DefaultDirName={userpf}\BulkEthernetConfigUploader
DefaultGroupName=BulkEthernetConfigUploader
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=BulkEthernetConfigUploader-{#Version}-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
LicenseFile=..\LICENSE
InfoBeforeFile=..\certs\NOTICE.txt
UninstallDisplayIcon={app}\BulkEthernetConfigUploader.exe

[Files]
Source: "..\dist\BulkEthernetConfigUploader\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\BulkEthernetConfigUploader"; Filename: "{app}\BulkEthernetConfigUploader.exe"
Name: "{group}\Uninstall BulkEthernetConfigUploader"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\BulkEthernetConfigUploader.exe"; Description: "Start BulkEthernetConfigUploader"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\logs"
