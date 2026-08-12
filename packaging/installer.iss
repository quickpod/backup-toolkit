; Inno Setup — Backup Toolkit. Signed single-file installer, compiled in CI.
#define AppName "Backup Toolkit"
#define AppVersion "1.0.3"

[Setup]
AppMutex=QuickOpen.BackupToolkit
AppId={{3F6F7C20-6D48-4E5B-8C71-9B0E2F3A4D56}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=QuickOpen (quickopen.ai)
AppPublisherURL=https://quickopen.ai/projects/backup-toolkit
DefaultDirName={autopf}\BackupToolkit
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\BackupToolkit.exe
OutputDir=dist
OutputBaseFilename=BackupToolkit-Setup
SetupIconFile=..\backup-toolkit.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
WizardImageFile=branding\wizard-large.bmp
WizardSmallImageFile=branding\wizard-small.bmp
AppCopyright=Apache-2.0. 100%% AI-built, published on QuickOpen (quickopen.ai).
VersionInfoCompany=QuickOpen
VersionInfoProductName=Backup Toolkit
VersionInfoVersion=1.0.3.0
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=Backup Toolkit is a 100%% AI-built, open-source offline tool, published on QuickOpen (quickopen.ai).%n%nThis will install it on your computer.
BeveledLabel=QuickOpen · quickopen.ai

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "trustca"; Description: "Trust the QuickOpen Root CA (lets Windows verify QuickOpen signatures)"; GroupDescription: "Security:"; Flags: unchecked

[Files]
Source: "staging\BackupToolkit.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "staging\quickopen-root.crt"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "staging\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme skipifsourcedoesntexist
Source: "staging\LICENSE"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\Backup Toolkit"; Filename: "{app}\BackupToolkit.exe"; IconFilename: "{app}\BackupToolkit.exe"
Name: "{group}\Uninstall Backup Toolkit"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Backup Toolkit"; Filename: "{app}\BackupToolkit.exe"; IconFilename: "{app}\BackupToolkit.exe"; Tasks: desktopicon

[Run]
Filename: "certutil.exe"; Parameters: "-addstore -user Root ""{app}\quickopen-root.crt"""; Tasks: trustca; Flags: runhidden; StatusMsg: "Trusting the QuickOpen Root CA..."
Filename: "{app}\BackupToolkit.exe"; Description: "Launch Backup Toolkit now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\BackupToolkit"

