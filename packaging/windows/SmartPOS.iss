#define MyAppName "SmartPOS"
#define MyAppVersion "0.1.0"

[Setup]
AppId={{8D7C2D6A-51B2-4A50-A6C1-7D9D1A12F001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=SmartPOS
DefaultDirName={autopf}\SmartPOS
DefaultGroupName=SmartPOS
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=admin
OutputDir=..\..\release
OutputBaseFilename=SmartPOS-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\SmartPOS.exe

[Files]
Source: "..\..\dist\SmartPOS.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\SmartPOS_Server.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\SmartPOS"; Filename: "{app}\SmartPOS.exe"
Name: "{autodesktop}\SmartPOS"; Filename: "{app}\SmartPOS.exe"

[Run]
Filename: "{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Parameters: "/silent /install"; StatusMsg: "Installing Microsoft Edge WebView2 Runtime..."; Flags: waituntilterminated
