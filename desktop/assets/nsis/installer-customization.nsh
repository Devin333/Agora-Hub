# Hardened long-path installer extension. This file is loaded through the
# documented nsis.include hook; it does not replace electron-builder's script.

!macro customHeader
  !addincludedir "${AGORA_NSIS_TEMPLATES_ROOT}"
  !cd "${AGORA_NSIS_GENERATED_DIR}"
  !ifndef BUILD_UNINSTALLER
    !include "${AGORA_NSIS_INSTALLER_FUNCTIONS_NSH}"
  !endif
!macroend

!macro AgoraClearEnv NAME
  System::Call 'Kernel32::SetEnvironmentVariableW(w "${NAME}", p 0)i.r9'
!macroend

!macro AgoraClearMutableEnvironment
  !insertmacro AgoraClearEnv "AGORA_NSIS_EXTRACTOR"
  !insertmacro AgoraClearEnv "COMSPEC"
  !insertmacro AgoraClearEnv "NODE_OPTIONS"
  !insertmacro AgoraClearEnv "NODE_PATH"
  !insertmacro AgoraClearEnv "NODE_REPL_EXTERNAL_MODULE"
  !insertmacro AgoraClearEnv "NODE_EXTRA_CA_CERTS"
  !insertmacro AgoraClearEnv "NODE_ICU_DATA"
  !insertmacro AgoraClearEnv "NODE_V8_COVERAGE"
  !insertmacro AgoraClearEnv "OPENSSL_CONF"
  !insertmacro AgoraClearEnv "OPENSSL_MODULES"
  !insertmacro AgoraClearEnv "SSL_CERT_DIR"
  !insertmacro AgoraClearEnv "SSL_CERT_FILE"
  !insertmacro AgoraClearEnv "PATH"
  !insertmacro AgoraClearEnv "PATHEXT"
!macroend

!macro AgoraRequireSha512 FILE EXPECTED LABEL CODE
  ${StdUtils.HashFile} $R8 "SHA2-512" "${FILE}"
  ${if} $R8 != "${EXPECTED}"
    DetailPrint "Hardened installer pin mismatch: ${LABEL}"
    SetErrorLevel ${CODE}
    Quit
  ${endIf}
!macroend

!macro AgoraNormalizeChildExit
  StrCmp $R0 "error" 0 +2
    StrCpy $R0 70
  StrCmp $R0 "timeout" 0 +2
    StrCpy $R0 124
!macroend

!macro AgoraFailChild PHASE
  !insertmacro AgoraNormalizeChildExit
  DetailPrint "Hardened installer bootstrap failed (${PHASE}), exit=$R0: $R1"
  SetErrorLevel $R0
  Quit
!macroend

!macro AgoraEmbedInstallerInputs
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "/oname=agora-node.exe" "${AGORA_NSIS_NODE_SOURCE}"
  File "/oname=agora-7za.exe" "${AGORA_NSIS_7ZA_SOURCE}"
  File "/oname=agora-bootstrap.cjs" "${AGORA_NSIS_BOOTSTRAP_SOURCE}"
  File "/oname=agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SOURCE}"
  File "/oname=agora-app.7z" "${APP_64}"
!macroend

!macro AgoraVerifyInstallerInputs
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-node.exe" "${AGORA_NSIS_NODE_SHA512}" "stage Node" 61
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-7za.exe" "${AGORA_NSIS_7ZA_SHA512}" "modern 7za" 61
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-bootstrap.cjs" "${AGORA_NSIS_BOOTSTRAP_SHA512}" "bootstrap" 61
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SHA512}" "payload manifest" 61
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-app.7z" "${AGORA_NSIS_ARCHIVE_SHA512}" "payload archive" 61
!macroend

!macro AgoraEmbedUninstallerInputs
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "/oname=agora-node.exe" "${AGORA_NSIS_NODE_SOURCE}"
  File "/oname=agora-bootstrap.cjs" "${AGORA_NSIS_BOOTSTRAP_SOURCE}"
  File "/oname=agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SOURCE}"
!macroend

!macro AgoraVerifyUninstallerInputs
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-node.exe" "${AGORA_NSIS_NODE_SHA512}" "stage Node" 61
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-bootstrap.cjs" "${AGORA_NSIS_BOOTSTRAP_SHA512}" "bootstrap" 61
  !insertmacro AgoraRequireSha512 "$PLUGINSDIR\agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SHA512}" "payload manifest" 61
!macroend

!macro customUnInit
  !insertmacro AgoraEmbedUninstallerInputs
  !insertmacro AgoraVerifyUninstallerInputs
!macroend

!macro customRemoveFiles
  SetOutPath "$TEMP"
  !insertmacro AgoraClearMutableEnvironment
  nsExec::ExecToStack /TIMEOUT=960000 '"$PLUGINSDIR\agora-node.exe" --no-addons --disable-proto=throw "$PLUGINSDIR\agora-bootstrap.cjs" uninstall "$PLUGINSDIR\agora-payload-manifest.json" "${AGORA_NSIS_PAYLOAD_MANIFEST_SHA512}" "$INSTDIR" "${AGORA_NSIS_PRODUCT_ID}" "$EXEPATH" "$PLUGINSDIR\agora-uninstall-result.json"'
  Pop $R0
  Pop $R1
  StrCmp $R0 "0" agora_remove_ok
  !insertmacro AgoraFailChild "remove"
  agora_remove_ok:
!macroend
