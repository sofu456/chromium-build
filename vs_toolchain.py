#!/usr/bin/env python
# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import print_function

import glob
import json
import os
import pipes
import platform
import re
import shutil
import stat
import subprocess
import sys

from gn_helpers import ToGNString


script_dir = os.path.dirname(os.path.realpath(__file__))
json_data_file = os.path.join(script_dir, 'win_toolchain.json')


# Use MSVS2017 as the default toolchain.
CURRENT_DEFAULT_TOOLCHAIN_VERSION = '2017'


def SetEnvironmentAndGetRuntimeDllDirs():
  """Sets up os.environ to use the depot_tools VS toolchain with gyp, and
  returns the location of the VC runtime DLLs so they can be copied into
  the output directory after gyp generation.

  Return value is [x64path, x86path, 'Arm64Unused','ArmUnused'] or None. arm64path is
  generated separately because there are multiple folders for the arm64 VC
  runtime.
  """
  vs_runtime_dll_dirs = None
  depot_tools_win_toolchain = \
      bool(int(os.environ.get('DEPOT_TOOLS_WIN_TOOLCHAIN', '1')))
  # When running on a non-Windows host, only do this if the SDK has explicitly
  # been downloaded before (in which case json_data_file will exist).
  if ((sys.platform in ('win32', 'cygwin') or os.path.exists(json_data_file))
      and depot_tools_win_toolchain):
    if ShouldUpdateToolchain():
      if len(sys.argv) > 1 and sys.argv[1] == 'update':
        update_result = Update()
      else:
        update_result = Update(no_download=True)
      if update_result != 0:
        raise Exception('Failed to update, error code %d.' % update_result)
    with open(json_data_file, 'r') as tempf:
      toolchain_data = json.load(tempf)

    toolchain = toolchain_data['path']
    version = toolchain_data['version']
    win_sdk = toolchain_data.get('win_sdk')
    if not win_sdk:
      win_sdk = toolchain_data['win8sdk']
    wdk = toolchain_data['wdk']
    # TODO(scottmg): The order unfortunately matters in these. They should be
    # split into separate keys for x64/x86/arm64. (See CopyDlls call below).
    # http://crbug.com/345992
    vs_runtime_dll_dirs = toolchain_data['runtime_dirs']
    # The number of runtime_dirs in the toolchain_data was two (x64/x86) but
    # changed to three (x64/x86/arm64) and this code needs to handle both
    # possibilities, which can change independently from this code.
    if len(vs_runtime_dll_dirs) == 2:
      vs_runtime_dll_dirs.append('Arm64Unused')

    os.environ['GYP_MSVS_OVERRIDE_PATH'] = toolchain
    os.environ['GYP_MSVS_VERSION'] = version

    os.environ['WINDOWSSDKDIR'] = win_sdk
    os.environ['WDK_DIR'] = wdk
    # Include the VS runtime in the PATH in case it's not machine-installed.
    runtime_path = os.path.pathsep.join(vs_runtime_dll_dirs)
    os.environ['PATH'] = runtime_path + os.path.pathsep + os.environ['PATH']
  elif sys.platform == 'win32' and not depot_tools_win_toolchain:
    if not 'GYP_MSVS_OVERRIDE_PATH' in os.environ:
      os.environ['GYP_MSVS_OVERRIDE_PATH'] = DetectVisualStudioPath()
    if not 'GYP_MSVS_VERSION' in os.environ:
      os.environ['GYP_MSVS_VERSION'] = GetVisualStudioVersion()

    # When using an installed toolchain these files aren't needed in the output
    # directory in order to run binaries locally, but they are needed in order
    # to create isolates or the mini_installer. Copying them to the output
    # directory ensures that they are available when needed.
    bitness = platform.architecture()[0]
    # When running 64-bit python the x64 DLLs will be in System32
    # ARM64 binaries will not be available in the system directories because we
    # don't build on ARM64 machines.
    x64_path = 'System32' if bitness == '64bit' else 'Sysnative'
    x64_path = os.path.join(os.path.expandvars('%windir%'), x64_path)
    vs_runtime_dll_dirs = [x64_path,
                           os.path.join(os.path.expandvars('%windir%'),
                                        'SysWOW64'),
                           'Arm64Unused',
                           os.path.join(os.path.expandvars('%windir%'),
                                        'SysWOW64')]

  return vs_runtime_dll_dirs


def _RegistryGetValueUsingWinReg(key, value):
  """Use the _winreg module to obtain the value of a registry key.

  Args:
    key: The registry key.
    value: The particular registry value to read.
  Return:
    contents of the registry key's value, or None on failure.  Throws
    ImportError if _winreg is unavailable.
  """
  import _winreg
  try:
    root, subkey = key.split('\\', 1)
    assert root == 'HKLM'  # Only need HKLM for now.
    with _winreg.OpenKey(_winreg.HKEY_LOCAL_MACHINE, subkey) as hkey:
      return _winreg.QueryValueEx(hkey, value)[0]
  except WindowsError:
    return None


def _RegistryGetValue(key, value):
  try:
    return _RegistryGetValueUsingWinReg(key, value)
  except ImportError:
    raise Exception('The python library _winreg not found.')


def GetVisualStudioVersion():
  """Return GYP_MSVS_VERSION of Visual Studio.
  """
  return os.environ.get('GYP_MSVS_VERSION', CURRENT_DEFAULT_TOOLCHAIN_VERSION)


def DetectVisualStudioPath():
  """Return path to the GYP_MSVS_VERSION of Visual Studio.
  """

  # Note that this code is used from
  # build/toolchain/win/setup_toolchain.py as well.
  version_as_year = GetVisualStudioVersion()
  year_to_version = {
      '2017': '15.0',
      '2019': '16.0',
  }
  if version_as_year not in year_to_version:
    raise Exception(('Visual Studio version %s (from GYP_MSVS_VERSION)'
                     ' not supported. Supported versions are: %s') % (
                       version_as_year, ', '.join(year_to_version.keys())))

  # The VC++ >=2017 install location needs to be located using COM instead of
  # the registry. For details see:
  # https://blogs.msdn.microsoft.com/heaths/2016/09/15/changes-to-visual-studio-15-setup/
  # For now we use a hardcoded default with an environment variable override.
  for path in (
      os.environ.get('vs%s_install' % version_as_year),
      os.path.expandvars('%ProgramFiles(x86)%' +
                         '/Microsoft Visual Studio/%s/Enterprise' %
                         version_as_year),
      os.path.expandvars('%ProgramFiles(x86)%' +
                         '/Microsoft Visual Studio/%s/Professional' %
                         version_as_year),
      os.path.expandvars('%ProgramFiles(x86)%' +
                         '/Microsoft Visual Studio/%s/Community' %
                         version_as_year),
      os.path.expandvars('%ProgramFiles(x86)%' +
                         '/Microsoft Visual Studio/%s/Preview' %
                         version_as_year)):
    if path and os.path.exists(path):
      return path

  raise Exception(('Visual Studio Version %s (from GYP_MSVS_VERSION)'
                   ' not found.') % (version_as_year))


def _CopyRuntimeImpl(target, source, verbose=True):
  """Copy |source| to |target| if it doesn't already exist or if it needs to be
  updated (comparing last modified time as an approximate float match as for
  some reason the values tend to differ by ~1e-07 despite being copies of the
  same file... https://crbug.com/603603).
  """
  if (os.path.isdir(os.path.dirname(target)) and
      (not os.path.isfile(target) or
       abs(os.stat(target).st_mtime - os.stat(source).st_mtime) >= 0.01)):
    if verbose:
      print('Copying %s to %s...' % (source, target))
    if os.path.exists(target):
      # Make the file writable so that we can delete it now, and keep it
      # readable.
      os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
      os.unlink(target)
    shutil.copy2(source, target)
    # Make the file writable so that we can overwrite or delete it later,
    # keep it readable.
    os.chmod(target, stat.S_IWRITE | stat.S_IREAD)


def _CopyUCRTRuntime(target_dir, source_dir, target_cpu, dll_pattern, suffix):
  """Copy both the msvcp and vccorlib runtime DLLs, only if the target doesn't
  exist, but the target directory does exist."""
  if target_cpu == 'arm64':
    # Windows ARM64 VCRuntime is located at {toolchain_root}/VC/Redist/MSVC/
    # {x.y.z}/[debug_nonredist/]arm64/Microsoft.VC141.CRT/.
    platform_toolset = '141'
    if os.environ['GYP_MSVS_VERSION'] == '2019':
      platform_toolset = '142'
      
    vc_redist_root = FindVCRedistRoot()
    if suffix.startswith('.'):
      source_dir = os.path.join(vc_redist_root,
                                'arm64', 'Microsoft.VC' + platform_toolset +'.CRT')
    else:
      source_dir = os.path.join(vc_redist_root, 'debug_nonredist',
                                'arm64', 'Microsoft.VC' + platform_toolset +'.DebugCRT')
  for file_part in ('msvcp', 'vccorlib', 'vcruntime'):
    dll = dll_pattern % file_part
    target = os.path.join(target_dir, dll)
    source = os.path.join(source_dir, dll)
    _CopyRuntimeImpl(target, source)
  # Copy the UCRT files from the Windows SDK. This location includes the
  # api-ms-win-crt-*.dll files that are not found in the Windows directory.
  # These files are needed for component builds. If WINDOWSSDKDIR is not set
  # use the default SDK path. This will be the case when
  # DEPOT_TOOLS_WIN_TOOLCHAIN=0 and vcvarsall.bat has not been run.
  win_sdk_dir = os.path.normpath(
      os.environ.get('WINDOWSSDKDIR',
                     os.path.expandvars('%ProgramFiles(x86)%'
                                        '\\Windows Kits\\10')))
  # ARM64 doesn't have a redist for the ucrt DLLs because they are always
  # present in the OS.
  if target_cpu != 'arm64':
    # Starting with the 10.0.17763 SDK the ucrt files are in a version-named
    # directory - this handles both cases.
    redist_dir = os.path.join(win_sdk_dir, 'Redist')
    version_dirs = glob.glob(os.path.join(redist_dir, '10.*'))
    if len(version_dirs) > 0:
      version_dirs.sort(reverse=True)
      redist_dir = version_dirs[0]
    ucrt_dll_dirs = os.path.join(redist_dir, 'ucrt', 'DLLs', target_cpu)
    ucrt_files = glob.glob(os.path.join(ucrt_dll_dirs, 'api-ms-win-*.dll'))
    assert len(ucrt_files) > 0
    for ucrt_src_file in ucrt_files:
      file_part = os.path.basename(ucrt_src_file)
      ucrt_dst_file = os.path.join(target_dir, file_part)
      _CopyRuntimeImpl(ucrt_dst_file, ucrt_src_file, False)
  # We must copy ucrtbase.dll for x64/x86, and ucrtbased.dll for all CPU types.
  if target_cpu != 'arm64' or not suffix.startswith('.'):
    if not suffix.startswith('.'):
      # ucrtbased.dll is located at {win_sdk_dir}/bin/{a.b.c.d}/{target_cpu}/
      # ucrt/.
      sdk_redist_root = os.path.join(win_sdk_dir, 'bin')
      sdk_bin_sub_dirs = os.listdir(sdk_redist_root)
      # Select the most recent SDK if there are multiple versions installed.
      sdk_bin_sub_dirs.sort(reverse=True)
      for directory in sdk_bin_sub_dirs:
        sdk_redist_root_version = os.path.join(sdk_redist_root, directory)
        if not os.path.isdir(sdk_redist_root_version):
          continue
        if re.match('10\.\d+\.\d+\.\d+', directory):
          source_dir = os.path.join(sdk_redist_root_version, target_cpu, 'ucrt')
          break
    _CopyRuntimeImpl(os.path.join(target_dir, 'ucrtbase' + suffix),
                     os.path.join(source_dir, 'ucrtbase' + suffix))


def FindVCComponentRoot(component):
  """Find the most recent Tools or Redist or other directory in an MSVC install.
  Typical results are {toolchain_root}/VC/{component}/MSVC/{x.y.z}. The {x.y.z}
  version number part changes frequently so the highest version number found is
  used.
  """
  assert GetVisualStudioVersion() in ['2017', '2019']
  SetEnvironmentAndGetRuntimeDllDirs()
  assert ('GYP_MSVS_OVERRIDE_PATH' in os.environ)
  vc_component_msvc_root = os.path.join(os.environ['GYP_MSVS_OVERRIDE_PATH'],
      'VC', component, 'MSVC')
  vc_component_msvc_contents = os.listdir(vc_component_msvc_root)
  # Select the most recent toolchain if there are several.
  vc_component_msvc_contents.sort(reverse=True)
  for directory in vc_component_msvc_contents:
    if not os.path.isdir(os.path.join(vc_component_msvc_root, directory)):
      continue
    if re.match('14\.\d+\.\d+', directory):
      return os.path.join(vc_component_msvc_root, directory)
  raise Exception('Unable to find the VC %s directory.' % component)


def FindVCRedistRoot():
  """In >=VS2017, Redist binaries are located in
  {toolchain_root}/VC/Redist/MSVC/{x.y.z}/{target_cpu}/.

  This returns the '{toolchain_root}/VC/Redist/MSVC/{x.y.z}/' path.
  """
  return FindVCComponentRoot('Redist')


def _CopyRuntime(target_dir, source_dir, target_cpu, debug):
  """Copy the VS runtime DLLs, only if the target doesn't exist, but the target
  directory does exist. Handles VS 2015, 2017 and 2019."""
  suffix = 'd.dll' if debug else '.dll'
  # VS 2015, 2017 and 2019 use the same CRT DLLs.
  _CopyUCRTRuntime(target_dir, source_dir, target_cpu, '%s140' + suffix,
                    suffix)


def CopyDlls(target_dir, configuration, target_cpu):
  """Copy the VS runtime DLLs into the requested directory as needed.

  configuration is one of 'Debug' or 'Release'.
  target_cpu is one of 'x86', 'x64' or 'arm64'.

  The debug configuration gets both the debug and release DLLs; the
  release config only the latter.
  """
  vs_runtime_dll_dirs = SetEnvironmentAndGetRuntimeDllDirs()
  if not vs_runtime_dll_dirs:
    return

  x64_runtime, x86_runtime, arm64_runtime, arm_runtime = vs_runtime_dll_dirs
  if target_cpu == 'x64':
    runtime_dir = x64_runtime
  elif target_cpu == 'x86':
    runtime_dir = x86_runtime
  elif target_cpu == 'arm':
    runtime_dir = arm_runtime
  elif target_cpu == 'arm64':
    runtime_dir = arm64_runtime
  else:
    raise Exception('Unknown target_cpu: ' + str(vs_runtime_dll_dirs))
  _CopyRuntime(target_dir, runtime_dir, target_cpu, debug=False)
  if configuration == 'Debug':
    _CopyRuntime(target_dir, runtime_dir, target_cpu, debug=True)

  _CopyDebugger(target_dir, target_cpu)


def _CopyDebugger(target_dir, target_cpu):
  """Copy dbghelp.dll and dbgcore.dll into the requested directory as needed.

  target_cpu is one of 'x86', 'x64' or 'arm64'.

  dbghelp.dll is used when Chrome needs to symbolize stacks. Copying this file
  from the SDK directory avoids using the system copy of dbghelp.dll which then
  ensures compatibility with recent debug information formats, such as VS
  2017 /debug:fastlink PDBs.

  dbgcore.dll is needed when using some functions from dbghelp.dll (like
  MinidumpWriteDump).
  """
  win_sdk_dir = SetEnvironmentAndGetSDKDir()
  if not win_sdk_dir:
    return

  # List of debug files that should be copied, the first element of the tuple is
  # the name of the file and the second indicates if it's optional.
  debug_files = [('dbghelp.dll', False), ('dbgcore.dll', True)]
  for debug_file, is_optional in debug_files:
    full_path = os.path.join(win_sdk_dir, 'Debuggers', target_cpu, debug_file)
    if not os.path.exists(full_path):
      if is_optional:
        continue
      else:
        # TODO(crbug.com/773476): remove version requirement.
        raise Exception('%s not found in "%s"\r\nYou must install the '
                        '"Debugging Tools for Windows" feature from the Windows'
                        ' 10 SDK.'
                        % (debug_file, full_path))
    target_path = os.path.join(target_dir, debug_file)
    _CopyRuntimeImpl(target_path, full_path)


def _GetDesiredVsToolchainHashes():
  """Load a list of SHA1s corresponding to the toolchains that we want installed
  to build with."""
  env_version = GetVisualStudioVersion()
  if env_version == '2017':
    # VS 2017 Update 9 (15.9.3) with 10.0.17763.132 SDK, 10.0.17134 version of
    # d3dcompiler_47.dll, with ARM64 libraries.
    toolchain_hash = '818a152b3f1da991c1725d85be19a0f27af6bab4'
    # Third parties that do not have access to the canonical toolchain can map
    # canonical toolchain version to their own toolchain versions.
    toolchain_hash_mapping_key = 'GYP_MSVS_HASH_%s' % toolchain_hash
    return [os.environ.get(toolchain_hash_mapping_key, toolchain_hash)]
  raise Exception('Unsupported VS version %s' % env_version)


def ShouldUpdateToolchain():
  """Check if the toolchain should be upgraded."""
  if not os.path.exists(json_data_file):
    return True
  with open(json_data_file, 'r') as tempf:
    toolchain_data = json.load(tempf)
  version = toolchain_data['version']
  env_version = GetVisualStudioVersion()
  # If there's a mismatch between the version set in the environment and the one
  # in the json file then the toolchain should be updated.
  return version != env_version


def Update(force=False, no_download=False):
  """Requests an update of the toolchain to the specific hashes we have at
  this revision. The update outputs a .json of the various configuration
  information required to pass to gyp which we use in |GetToolchainDir()|.
  If no_download is true then the toolchain will be configured if present but
  will not be downloaded.
  """
  if force != False and force != '--force':
    print('Unknown parameter "%s"' % force, file=sys.stderr)
    return 1
  if force == '--force' or os.path.exists(json_data_file):
    force = True

  depot_tools_win_toolchain = \
      bool(int(os.environ.get('DEPOT_TOOLS_WIN_TOOLCHAIN', '1')))
  if ((sys.platform in ('win32', 'cygwin') or force) and
        depot_tools_win_toolchain):
    import find_depot_tools
    depot_tools_path = find_depot_tools.add_depot_tools_to_path()

    # On Linux, the file system is usually case-sensitive while the Windows
    # SDK only works on case-insensitive file systems.  If it doesn't already
    # exist, set up a ciopfs fuse mount to put the SDK in a case-insensitive
    # part of the file system.
    toolchain_dir = os.path.join(depot_tools_path, 'win_toolchain', 'vs_files')
    # For testing this block, unmount existing mounts with
    # fusermount -u third_party/depot_tools/win_toolchain/vs_files
    if sys.platform.startswith('linux') and not os.path.ismount(toolchain_dir):
      import distutils.spawn
      ciopfs = distutils.spawn.find_executable('ciopfs')
      if not ciopfs:
        # ciopfs not found in PATH; try the one downloaded from the DEPS hook.
        ciopfs = os.path.join(script_dir, 'ciopfs')
      if not os.path.isdir(toolchain_dir):
        os.mkdir(toolchain_dir)
      if not os.path.isdir(toolchain_dir + '.ciopfs'):
        os.mkdir(toolchain_dir + '.ciopfs')
      # Without use_ino, clang's #pragma once and Wnonportable-include-path
      # both don't work right, see https://llvm.org/PR34931
      # use_ino doesn't slow down builds, so it seems there's no drawback to
      # just using it always.
      subprocess.check_call([
          ciopfs, '-o', 'use_ino', toolchain_dir + '.ciopfs', toolchain_dir])

    # Necessary so that get_toolchain_if_necessary.py will put the VS toolkit
    # in the correct directory.
    os.environ['GYP_MSVS_VERSION'] = GetVisualStudioVersion()
    get_toolchain_args = [
        sys.executable,
        os.path.join(depot_tools_path,
                    'win_toolchain',
                    'get_toolchain_if_necessary.py'),
        '--output-json', json_data_file,
      ] + _GetDesiredVsToolchainHashes()
    if force:
      get_toolchain_args.append('--force')
    if no_download:
      get_toolchain_args.append('--no-download')
    subprocess.check_call(get_toolchain_args)

  return 0


def NormalizePath(path):
  while path.endswith('\\'):
    path = path[:-1]
  return path


def SetEnvironmentAndGetSDKDir():
  """Gets location information about the current sdk (must have been
  previously updated by 'update'). This is used for the GN build."""
  SetEnvironmentAndGetRuntimeDllDirs()

  # If WINDOWSSDKDIR is not set, search the default SDK path and set it.
  if not 'WINDOWSSDKDIR' in os.environ:
    default_sdk_path = os.path.expandvars('%ProgramFiles(x86)%'
                                          '\\Windows Kits\\10')
    if os.path.isdir(default_sdk_path):
      os.environ['WINDOWSSDKDIR'] = default_sdk_path

  return NormalizePath(os.environ['WINDOWSSDKDIR'])


def GetToolchainDir():
  """Gets location information about the current toolchain (must have been
  previously updated by 'update'). This is used for the GN build."""
  runtime_dll_dirs = SetEnvironmentAndGetRuntimeDllDirs()
  win_sdk_dir = SetEnvironmentAndGetSDKDir()

  print('''vs_path = %s
sdk_path = %s
vs_version = %s
wdk_dir = %s
runtime_dirs = %s
''' % (ToGNString(NormalizePath(os.environ['GYP_MSVS_OVERRIDE_PATH'])),
       ToGNString(win_sdk_dir), ToGNString(GetVisualStudioVersion()),
       ToGNString(NormalizePath(os.environ.get('WDK_DIR', ''))),
       ToGNString(os.path.pathsep.join(runtime_dll_dirs or ['None']))))


def main():
  commands = {
      'update': Update,
      'get_toolchain_dir': GetToolchainDir,
      'copy_dlls': CopyDlls,
  }
  if len(sys.argv) < 2 or sys.argv[1] not in commands:
    print('Expected one of: %s' % ', '.join(commands), file=sys.stderr)
    return 1
  return commands[sys.argv[1]](*sys.argv[2:])


if __name__ == '__main__':
  sys.exit(main())
