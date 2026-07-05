[Setup]
AppName=Framecycler
AppVersion=1.0.0
DefaultDirName={autopf}\Framecycler
DefaultGroupName=Framecycler
UninstallDisplayIcon={app}\Framecycler.exe
Compression=lzma2
SolidCompression=yes
OutputDir=dist
OutputBaseFilename=Framecycler-Installer
DisableProgramGroupPage=yes
WizardStyle=modern

[Files]
Source: "dist\Framecycler\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Framecycler"; Filename: "{app}\Framecycler.exe"
Name: "{autodesktop}\Framecycler"; Filename: "{app}\Framecycler.exe"

[Run]
Filename: "{app}\Framecycler.exe"; Description: "Launch Framecycler"; Flags: postinstall nowait skipifsilent
