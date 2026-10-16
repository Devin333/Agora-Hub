# Included inside stock installSection by the generated installer.nsh shim.
# This file intentionally contains only macro redefinitions; NSIS Function
# declarations are illegal at this Section-local include point.

  # Replace only the embedded-package install macro so payload verification,
  # uninstaller embedding and the sealed ownership record all complete before
  # stock registry/shortcut/file-association actions.
  !macroundef installApplicationFiles
  !macro installApplicationFiles
    !insertmacro extractEmbeddedAppPackage
    # Preserve the locked upstream embedded-installer store behavior.
    ${if} $installMode == "all"
      SetShellVarContext current
    ${endif}
    !insertmacro copyFile "$EXEPATH" "$LOCALAPPDATA\${APP_INSTALLER_STORE_FILE}"
    ${if} $installMode == "all"
      SetShellVarContext all
    ${endif}
    File "/oname=${AGORA_NSIS_UNINSTALLER_FILENAME}" "${UNINSTALLER_OUT_FILE}"
    Call AgoraSealInstallation
  !macroend

  !macroundef x64_app_files
  !macro x64_app_files
  !macroend

  !macroundef extractUsing7za
  !macro extractUsing7za FILE
    Call AgoraInstallPayload
  !macroend
