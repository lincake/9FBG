@echo off
setlocal
set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
  echo ERROR: .NET Framework x86 C# compiler not found: "%CSC%" 1>&2
  exit /b 2
)
pushd "%~dp0"
"%CSC%" /nologo /target:exe /platform:x86 /optimize+ /checked+ /r:System.Management.dll /out:BaySpecSdkStream.exe BaySpecSdkStream.cs
set "BUILD_RESULT=%ERRORLEVEL%"
popd
exit /b %BUILD_RESULT%
