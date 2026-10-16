# Included globally by customHeader for the installer build only.

Var /GLOBAL agoraPreflightDone

  Function AgoraPreflightOnce
    StrCmp $agoraPreflightDone "1" agora_preflight_return
    !insertmacro AgoraEmbedInstallerInputs
    !insertmacro AgoraVerifyInstallerInputs
    !insertmacro AgoraClearMutableEnvironment
    nsExec::ExecToStack /TIMEOUT=960000 '"$PLUGINSDIR\agora-node.exe" --no-addons --disable-proto=throw "$PLUGINSDIR\agora-bootstrap.cjs" preflight "$PLUGINSDIR\agora-7za.exe" "${AGORA_NSIS_7ZA_SHA256}" "$PLUGINSDIR\agora-app.7z" "${AGORA_NSIS_ARCHIVE_SHA512}" "$PLUGINSDIR\agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SHA512}" "$INSTDIR" "${AGORA_NSIS_PRODUCT_ID}" "$PLUGINSDIR\agora-preflight-result.json"'
    Pop $R0
    Pop $R1
    StrCmp $R0 "0" agora_preflight_ok
    !insertmacro AgoraFailChild "preflight"
    agora_preflight_ok:
    Call AgoraAdmitRegisteredOldInstallations
    StrCpy $agoraPreflightDone "1"
    agora_preflight_return:
  FunctionEnd

  Function AgoraAdmitOwnedRoot
    # $R8 is a registry InstallLocation and $R9 is its UninstallString.
    StrCmp $R9 "" agora_owned_none
    StrCmp $R8 "" 0 agora_owned_run
    DetailPrint "Hardened installer refuses an old uninstall registration without InstallLocation"
    SetErrorLevel 60
    Quit
    agora_owned_run:
    !insertmacro AgoraClearMutableEnvironment
    nsExec::ExecToStack /TIMEOUT=960000 '"$PLUGINSDIR\agora-node.exe" --no-addons --disable-proto=throw "$PLUGINSDIR\agora-bootstrap.cjs" admit-owned "$R8" "${AGORA_NSIS_PRODUCT_ID}" "$PLUGINSDIR\agora-owned-result.json"'
    Pop $R0
    Pop $R1
    StrCmp $R0 "0" agora_owned_none
    !insertmacro AgoraFailChild "owned-upgrade-admission"
    agora_owned_none:
  FunctionEnd

  Function AgoraAdmitRegisteredOldInstallations
    Push $R8
    Push $R9
    # Current-user registration is always considered by stock installSection.
    ReadRegStr $R8 HKEY_CURRENT_USER "${INSTALL_REGISTRY_KEY}" InstallLocation
    ReadRegStr $R9 HKEY_CURRENT_USER "${UNINSTALL_REGISTRY_KEY}" UninstallString
    Call AgoraAdmitOwnedRoot
    # Per-machine installSection also considers the machine registration first.
    ${if} $installMode == "all"
      ReadRegStr $R8 HKEY_LOCAL_MACHINE "${INSTALL_REGISTRY_KEY}" InstallLocation
      ReadRegStr $R9 HKEY_LOCAL_MACHINE "${UNINSTALL_REGISTRY_KEY}" UninstallString
      Call AgoraAdmitOwnedRoot
    ${endIf}
    Pop $R9
    Pop $R8
  FunctionEnd

  Function AgoraInstallPayload
    Call AgoraPreflightOnce
    !insertmacro AgoraClearMutableEnvironment
    nsExec::ExecToStack /TIMEOUT=960000 '"$PLUGINSDIR\agora-node.exe" --no-addons --disable-proto=throw "$PLUGINSDIR\agora-bootstrap.cjs" install "$PLUGINSDIR\agora-7za.exe" "${AGORA_NSIS_7ZA_SHA256}" "$PLUGINSDIR\agora-app.7z" "${AGORA_NSIS_ARCHIVE_SHA512}" "$PLUGINSDIR\agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SHA512}" "$INSTDIR" "${AGORA_NSIS_PRODUCT_ID}" "$PLUGINSDIR\agora-install-result.json"'
    Pop $R0
    Pop $R1
    StrCmp $R0 "0" agora_install_ok
    !insertmacro AgoraFailChild "install"
    agora_install_ok:
    SetOutPath "$INSTDIR"
  FunctionEnd

  Function AgoraSealInstallation
    !insertmacro AgoraClearMutableEnvironment
    nsExec::ExecToStack /TIMEOUT=960000 '"$PLUGINSDIR\agora-node.exe" --no-addons --disable-proto=throw "$PLUGINSDIR\agora-bootstrap.cjs" seal "$PLUGINSDIR\agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SHA512}" "$INSTDIR" "${AGORA_NSIS_PRODUCT_ID}" "$PLUGINSDIR\agora-seal-result.json"'
    Pop $R0
    Pop $R1
    StrCmp $R0 "0" agora_seal_ok
    !insertmacro AgoraFailChild "seal"
    agora_seal_ok:
  FunctionEnd
