; Installeur Windows de Trip Compta (Inno Setup 6) : fabriqué par build_tools/installeur_windows.py, après
; build_tools/build.py (dossier dist\TripCompta). Résultat : dist\TripCompta-windows-setup.exe.
;
; - Installation pour l'utilisateur seul (pas de droits administrateur) : %LOCALAPPDATA%\Programs\TripCompta,
;   raccourcis menu Démarrer (et Bureau), désinstalleur dans « Applications installées ». Les voyages (Documents\
;   TripCompta ou le dossier choisi) ne sont jamais touchés, ni par l'installation ni par la désinstallation.
; - Fichiers installés par l'installeur : pas de marque « téléchargé depuis Internet » (.NET les charge sans
;   « Débloquer »).
; - WebView2 (la fenêtre de Trip Compta) installé s'il manque (Windows 10 ancien).
; - Mise à jour (lancée par Trip Compta lui-même, compta/updater.py) :
;     TripCompta-windows-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /ATTENDRE=<pid> /ECHEC=<fichier>
;                                  /ANCIEN=<programme actuel>
;   attend la fin de l'application, met l'ancienne version de côté (dossier « TripCompta.ancien »), installe,
;   relance « TripCompta.exe --apres-maj » et attend que la nouvelle version dise qu'elle a démarré (fichier
;   « demarre.txt » à côté de <fichier>, écrit par compta/updater.py : confirm_started ; /ATTENTE=<secondes>, 120
;   par défaut). Si l'installation échoue ou si la nouvelle version ne démarre pas : l'ancienne est remise à sa
;   place, un message est écrit dans <fichier> et l'ancienne version est relancée.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef SourceDir
  #define SourceDir "..\..\dist\TripCompta"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

[Setup]
AppId={{6F3C2A91-5B7E-4D2A-9C1E-7A4B8D2E5F10}
AppName=Trip Compta
AppVersion={#AppVersion}
AppVerName=Trip Compta {#AppVersion}
AppPublisher=Trip Compta
AppPublisherURL=https://github.com/Scendari/tripcompta
AppSupportURL=https://github.com/Scendari/tripcompta/blob/main/INSTALLER.md
DefaultDirName={localappdata}\Programs\TripCompta
DefaultGroupName=Trip Compta
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=TripCompta-windows-setup
SetupIconFile=..\icone\TripCompta.ico
UninstallDisplayIcon={app}\TripCompta.exe
UninstallDisplayName=Trip Compta
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "bureau"; Description: "Mettre un raccourci sur le Bureau"

[InstallDelete]
; l'ancienne version d'abord : aucun fichier périmé ne reste mélangé à la nouvelle
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist; Check: NeedsWebView2

[Icons]
Name: "{userprograms}\Trip Compta"; Filename: "{app}\TripCompta.exe"
Name: "{userdesktop}\Trip Compta"; Filename: "{app}\TripCompta.exe"; Tasks: bureau

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Installation de WebView2 (la fenêtre de Trip Compta)…"; Flags: waituntilterminated skipifdoesntexist; Check: NeedsWebView2
Filename: "{app}\TripCompta.exe"; Description: "Ouvrir Trip Compta"; Flags: nowait postinstall skipifsilent
; (mise à jour : la relance est faite par le code, StartAfterUpdate, qui vérifie que la nouvelle version démarre)

[Code]
const
  SYNCHRONIZE = $00100000;
  WEBVIEW2 = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  { l'inscription dans « Applications installées » (même identifiant que AppId, suffixe _is1 d'Inno Setup) }
  UNINSTALL_KEY = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F3C2A91-5B7E-4D2A-9C1E-7A4B8D2E5F10}_is1';

function OpenProcess(dwDesiredAccess: Cardinal; bInheritHandle: Boolean; dwProcessId: Cardinal): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(hHandle: THandle; dwMilliseconds: Cardinal): Cardinal;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

var
  Installed: Boolean;
  AppDir: String;      { le dossier de l'application (connu à partir de PrepareToInstall) }
  Backup: String;      { mise à jour : l'ancienne version, mise de côté le temps d'installer ('' : aucune) }
  OldVersion: String;  { son numéro dans « Applications installées », remis si on revient à elle }

function IsUpdate: Boolean;
begin
  Result := ExpandConstant('{param:ATTENDRE|}') <> '';
end;

{ Dossier de travail de la mise à jour : celui du fichier /ECHEC ('' si l'option n'est pas donnée) }
function WorkDir: String;
begin
  Result := ExtractFileDir(ExpandConstant('{param:ECHEC|}'));
end;

procedure SayFailure(Text: String);
var
  Msg: TArrayOfString;
begin
  if ExpandConstant('{param:ECHEC|}') <> '' then
  begin
    SetArrayLength(Msg, 1);
    Msg[0] := Text;
    SaveStringsToUTF8File(ExpandConstant('{param:ECHEC|}'), Msg, False);
  end;
end;

{ Remet l'ancienne version à sa place (la nouvelle, incomplète ou qui ne démarre pas, est retirée) }
function RestoreOld: Boolean;
var
  I: Integer;
  Moved: Boolean;
begin
  Result := False;
  if (Backup = '') or (AppDir = '') then
    Exit;
  if DirExists(AppDir) then
  begin
    if DirExists(AppDir + '.rate') then
      DelTree(AppDir + '.rate', True, True, True);
    Moved := False;
    for I := 1 to 10 do        { un programme tout juste arrêté garde ses fichiers encore un instant }
    begin
      Moved := RenameFile(AppDir, AppDir + '.rate');
      if Moved then
        Break;
      Sleep(1000);
    end;
    if not Moved then
    begin
      Log('Impossible d''écarter la nouvelle version : l''ancienne reste dans ' + Backup);
      Exit;
    end;
  end;
  if RenameFile(Backup, AppDir) then
  begin
    Log('Ancienne version remise en place');
    if OldVersion <> '' then
    begin
      { l'installation a déjà écrit le nouveau numéro : « Applications installées » doit montrer celui d'avant }
      if RegWriteStringValue(HKA, UNINSTALL_KEY, 'DisplayVersion', OldVersion) then
        Log('Numéro remis dans « Applications installées » : ' + OldVersion)
      else
        Log('Impossible de remettre le numéro ' + OldVersion + ' dans « Applications installées »');
    end;
    Result := True;
    Backup := '';
    DelTree(AppDir + '.rate', True, True, True);
  end
  else
  begin
    Log('Impossible de remettre l''ancienne version : la nouvelle est laissée en place');
    RenameFile(AppDir + '.rate', AppDir);
  end;
end;

{ Arrête le Trip Compta de CE dossier (une nouvelle version qui ne répond pas), et lui seul }
procedure StopApp(Exe: String);
var
  Rc: Integer;
  P: String;
begin
  P := Exe;
  StringChangeEx(P, '''', '''''', True);
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -Command "Get-Process TripCompta -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq ''' + P + ''' } | Stop-Process -Force"',
       '', SW_HIDE, ewWaitUntilTerminated, Rc);
  Sleep(2000);   { Windows libère les fichiers un peu après la fin du programme }
end;

{ Mise à jour installée : on lance la nouvelle version et on attend qu'elle dise « j'ai démarré » }
procedure StartAfterUpdate;
var
  Exe, Marker: String;
  Rc, I, Secs: Integer;
  Ok: Boolean;
begin
  Exe := AppDir + '\TripCompta.exe';
  Exec(Exe, '--apres-maj', AppDir, SW_SHOWNORMAL, ewNoWait, Rc);
  if (Backup = '') or (WorkDir = '') then
    Exit;                      { rien à remettre en place : on ne peut que lancer }
  Marker := WorkDir + '\demarre.txt';
  Secs := StrToIntDef(ExpandConstant('{param:ATTENTE|120}'), 120);
  Ok := False;
  for I := 1 to Secs * 2 do
  begin
    if FileExists(Marker) then
    begin
      Ok := True;
      Break;
    end;
    Sleep(500);
  end;
  if Ok then
  begin
    Log('La nouvelle version a démarré : l''ancienne est effacée');
    DelTree(Backup, True, True, True);
    Backup := '';
    Exit;
  end;
  Log('La nouvelle version n''a pas démarré : retour à l''ancienne');
  StopApp(Exe);
  if RestoreOld then
  begin
    SayFailure('La nouvelle version n''a pas démarré correctement. L''ancienne version a été remise en place.');
    Exec(Exe, '--apres-maj', AppDir, SW_SHOWNORMAL, ewNoWait, Rc);
  end
  else
    SayFailure('La nouvelle version n''a pas démarré correctement. Réinstalle Trip Compta depuis le site : tes voyages ne sont pas touchés.');
end;

function HasWebView2(Root: Integer; Key: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function NeedsWebView2: Boolean;
begin
  Result := not (HasWebView2(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2)
                 or HasWebView2(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2)
                 or HasWebView2(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WEBVIEW2));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Pid: Integer;
  H: THandle;
begin
  Result := '';
  Pid := StrToIntDef(ExpandConstant('{param:ATTENDRE|0}'), 0);
  if Pid > 0 then
  begin
    Log(Format('Attente de la fermeture de Trip Compta (%d)', [Pid]));
    H := OpenProcess(SYNCHRONIZE, False, Pid);
    if H <> 0 then
    begin
      if WaitForSingleObject(H, 60000) <> 0 then
        Log('Trip Compta ne s''est pas fermé en 60 s');
      CloseHandle(H);
    end;
    Sleep(1000);   { Windows libère les fichiers un peu après la fin du programme }
  end;
  AppDir := WizardDirValue;
  if IsUpdate then
  begin
    { l'ancienne version est mise de côté : si la nouvelle ne s'installe pas ou ne démarre pas, on la remet }
    if WorkDir <> '' then
      DeleteFile(WorkDir + '\demarre.txt');
    if DirExists(AppDir + '.ancien') then
      DelTree(AppDir + '.ancien', True, True, True);
    if DirExists(AppDir) and RenameFile(AppDir, AppDir + '.ancien') then
    begin
      Backup := AppDir + '.ancien';
      Log('Ancienne version mise de côté : ' + Backup);
      if not RegQueryStringValue(HKA, UNINSTALL_KEY, 'DisplayVersion', OldVersion) then
        OldVersion := '';
    end
    else
      Log('Ancienne version pas mise de côté (dossier absent ou occupé) : installation par-dessus');
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    Installed := True;
    if IsUpdate then
      StartAfterUpdate;
  end;
end;

procedure DeinitializeSetup;
var
  Old: String;
  Rc: Integer;
begin
  if IsUpdate and not Installed then
  begin
    Log('Mise à jour pas installée : relance de l''ancienne version');
    if Backup <> '' then
      RestoreOld;
    SayFailure('La mise à jour n''a pas pu s''installer. Rien n''a été changé : c''est toujours l''ancienne version.');
    Old := ExpandConstant('{param:ANCIEN|}');
    if (Old <> '') and FileExists(Old) then
      Exec(Old, '--apres-maj', ExtractFileDir(Old), SW_SHOWNORMAL, ewNoWait, Rc);
  end;
end;
