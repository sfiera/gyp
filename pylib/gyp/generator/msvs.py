#!/usr/bin/python

# Copyright (c) 2009 Google Inc. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import ntpath
import posixpath
import os
import re
import subprocess
import sys

import gyp.MSVSNew as MSVSNew
import gyp.MSVSProject as MSVSProject
import gyp.MSVSToolFile as MSVSToolFile
import gyp.MSVSUserFile as MSVSUserFile
import gyp.MSVSVersion as MSVSVersion
import gyp.common


# Regular expression for validating Visual Studio GUIDs.  If the GUID
# contains lowercase hex letters, MSVS will be fine. However,
# IncrediBuild BuildConsole will parse the solution file, but then
# silently skip building the target causing hard to track down errors.
# Note that this only happens with the BuildConsole, and does not occur
# if IncrediBuild is executed from inside Visual Studio.  This regex
# validates that the string looks like a GUID with all uppercase hex
# letters.
VALID_MSVS_GUID_CHARS = re.compile('^[A-F0-9\-]+$')


generator_default_variables = {
    'EXECUTABLE_PREFIX': '',
    'EXECUTABLE_SUFFIX': '.exe',
    'STATIC_LIB_PREFIX': '',
    'SHARED_LIB_PREFIX': '',
    'STATIC_LIB_SUFFIX': '.lib',
    'SHARED_LIB_SUFFIX': '.dll',
    'INTERMEDIATE_DIR': '$(IntDir)',
    'SHARED_INTERMEDIATE_DIR': '$(OutDir)/obj/global_intermediate',
    'OS': 'win',
    'PRODUCT_DIR': '$(OutDir)',
    'LIB_DIR': '$(OutDir)/lib',
    'RULE_INPUT_ROOT': '$(InputName)',
    'RULE_INPUT_EXT': '$(InputExt)',
    'RULE_INPUT_NAME': '$(InputFileName)',
    'RULE_INPUT_PATH': '$(InputPath)',
    'CONFIGURATION_NAME': '$(ConfigurationName)',
}


# The msvs specific sections that hold paths
generator_additional_path_sections = [
    'msvs_cygwin_dirs',
    'msvs_props',
]

generator_additional_non_configuration_keys = [
    'msvs_cygwin_dirs',
    'msvs_cygwin_shell',
]

# List of precompiled header related keys.
precomp_keys = [
    'msvs_precompiled_header',
    'msvs_precompiled_source',
]

cached_username = None
cached_domain = None

# TODO(gspencer): Switch the os.environ calls to be
# win32api.GetDomainName() and win32api.GetUserName() once the
# python version in depot_tools has been updated to work on Vista
# 64-bit.
def _GetDomainAndUserName():
  if sys.platform not in ('win32', 'cygwin'):
    return ('DOMAIN', 'USERNAME')
  global cached_username
  global cached_domain
  if not cached_domain or not cached_username:
    domain = os.environ.get('USERDOMAIN')
    username = os.environ.get('USERNAME')
    if not domain or not username:
      call = subprocess.Popen(['net', 'config', 'Workstation'],
                                stdout=subprocess.PIPE)
      config = call.communicate()[0]
      username_re = re.compile('^User name\s+(\S+)', re.MULTILINE)
      username_match = username_re.search(config)
      if username_match:
        username = username_match.group(1)
      domain_re = re.compile('^Logon domain\s+(\S+)', re.MULTILINE)
      domain_match = domain_re.search(config)
      if domain_match:
        domain = domain_match.group(1)
    cached_domain = domain
    cached_username = username
  return (cached_domain, cached_username)

fixpath_prefix = None


def _FixPath(path):
  """Convert paths to a form that will make sense in a vcproj file.

  Arguments:
    path: The path to convert, may contain / etc.
  Returns:
    The path with all slashes made into backslashes.
  """
  if fixpath_prefix and path and not os.path.isabs(path) and not path[0] == '$':
    path = os.path.join(fixpath_prefix, path)
  path = path.replace('/', '\\')
  if len(path) > 0 and path[-1] == '\\':
    path = path[:-1]
  return path


def _SourceInFolders(sources, prefix=None, excluded=None):
  """Converts a list split source file paths into a vcproj folder hierarchy.

  Arguments:
    sources: A list of source file paths split.
    prefix: A list of source file path layers meant to apply to each of sources.
  Returns:
    A hierarchy of filenames and MSVSProject.Filter objects that matches the
    layout of the source tree.
    For example:
    _SourceInFolders([['a', 'bob1.c'], ['b', 'bob2.c']], prefix=['joe'])
    -->
    [MSVSProject.Filter('a', contents=['joe\\a\\bob1.c']),
     MSVSProject.Filter('b', contents=['joe\\b\\bob2.c'])]
  """
  if not prefix: prefix = []
  result = []
  excluded_result = []
  folders = dict()
  # Gather files into the final result, excluded, or folders.
  for s in sources:
    if len(s) == 1:
      filename = '\\'.join(prefix + s)
      if filename in excluded:
        excluded_result.append(filename)
      else:
        result.append(filename)
    else:
      if not folders.get(s[0]):
        folders[s[0]] = []
      folders[s[0]].append(s[1:])
  # Add a folder for excluded files.
  if excluded_result:
    excluded_folder = MSVSProject.Filter('_excluded_files',
                                         contents=excluded_result)
    result.append(excluded_folder)
  # Populate all the folders.
  for f in folders:
    contents = _SourceInFolders(folders[f], prefix=prefix + [f],
                                excluded=excluded)
    contents = MSVSProject.Filter(f, contents=contents)
    result.append(contents)

  return result


def _ToolAppend(tools, tool_name, setting, value, only_if_unset=False):
  if not value: return
  # TODO(bradnelson): ugly hack, fix this more generally!!!
  if 'Directories' in setting or 'Dependencies' in setting:
    if type(value) == str:
      value = value.replace('/', '\\')
    else:
      value = [i.replace('/', '\\') for i in value]
  if not tools.get(tool_name):
    tools[tool_name] = dict()
  tool = tools[tool_name]
  if tool.get(setting):
    if only_if_unset: return
    if type(tool[setting]) == list:
      tool[setting] += value
    else:
      raise TypeError(
          'Appending "%s" to a non-list setting "%s" for tool "%s" is '
          'not allowed, previous value: %s' % (
              value, setting, tool_name, str(tool[setting])))
  else:
    tool[setting] = value


def _ConfigPlatform(config_data):
  return config_data.get('msvs_configuration_platform', 'Win32')


def _ConfigBaseName(config_name, platform_name):
  if config_name.endswith('_' + platform_name):
    return config_name[0:-len(platform_name)-1]
  else:
    return config_name


def _ConfigFullName(config_name, config_data):
  platform_name = _ConfigPlatform(config_data)
  return '%s|%s' % (_ConfigBaseName(config_name, platform_name), platform_name)


def _PrepareActionRaw(spec, cmd, cygwin_shell, has_input_path, quote_cmd):
  if cygwin_shell:
    # Find path to cygwin.
    cygwin_dir = _FixPath(spec.get('msvs_cygwin_dirs', ['.'])[0])
    # Prepare command.
    direct_cmd = cmd
    direct_cmd = [i.replace('$(IntDir)',
                            '`cygpath -m "${INTDIR}"`') for i in direct_cmd]
    direct_cmd = [i.replace('$(OutDir)',
                            '`cygpath -m "${OUTDIR}"`') for i in direct_cmd]
    if has_input_path:
      direct_cmd = [i.replace('$(InputPath)',
                              '`cygpath -m "${INPUTPATH}"`')
                    for i in direct_cmd]
    direct_cmd = ['"%s"' % i for i in direct_cmd]
    direct_cmd = [i.replace('"', '\\"') for i in direct_cmd]
    #direct_cmd = gyp.common.EncodePOSIXShellList(direct_cmd)
    direct_cmd = ' '.join(direct_cmd)
    # TODO(quote):  regularize quoting path names throughout the module
    cmd = (
      '"$(ProjectDir)%(cygwin_dir)s\\setup_env.bat" && '
      'set CYGWIN=nontsec&& ')
    if direct_cmd.find('NUMBER_OF_PROCESSORS') >= 0:
      cmd += 'set /a NUMBER_OF_PROCESSORS_PLUS_1=%%NUMBER_OF_PROCESSORS%%+1&& '
    if direct_cmd.find('INTDIR') >= 0:
      cmd += 'set INTDIR=$(IntDir)&& '
    if direct_cmd.find('OUTDIR') >= 0:
      cmd += 'set OUTDIR=$(OutDir)&& '
    if has_input_path and direct_cmd.find('INPUTPATH') >= 0:
      cmd += 'set INPUTPATH=$(InputPath) && '
    cmd += (
      'bash -c "%(cmd)s"')
    cmd = cmd % {'cygwin_dir': cygwin_dir,
                 'cmd': direct_cmd}
    return cmd
  else:
    # Convert cat --> type to mimic unix.
    if cmd[0] == 'cat':
      cmd = ['type'] + cmd[1:]
    if quote_cmd:
      # Support a mode for using cmd directly.
      # Convert any paths to native form (first element is used directly).
      # TODO(quote):  regularize quoting path names throughout the module
      direct_cmd = ([cmd[0].replace('/', '\\')] +
                    ['"%s"' % _FixPath(i) for i in cmd[1:]])
    else:
      direct_cmd = ([cmd[0].replace('/', '\\')] +
                    [_FixPath(i) for i in cmd[1:]])
    # Collapse into a single command.
    return ' '.join(direct_cmd)


def _PrepareAction(spec, rule, has_input_path):
  # Find path to cygwin.
  cygwin_dir = _FixPath(spec.get('msvs_cygwin_dirs', ['.'])[0])

  # Currently this weird argument munging is used to duplicate the way a
  # python script would need to be run as part of the chrome tree.
  # Eventually we should add some sort of rule_default option to set this
  # per project. For now the behavior chrome needs is the default.
  mcs = rule.get('msvs_cygwin_shell')
  if mcs is None:
    mcs = int(spec.get('msvs_cygwin_shell', 1))
  elif isinstance(mcs, str):
    mcs = int(mcs)
  quote_cmd = int(rule.get('msvs_quote_cmd', 1))
  return _PrepareActionRaw(spec, rule['action'], mcs,
                           has_input_path, quote_cmd)


def _PickPrimaryInput(inputs):
  # Pick second input as the primary one, unless there's only one.
  # TODO(bradnelson): this is a bit of a hack,
  # find something more general.
  if len(inputs) > 1:
    return inputs[1]
  else:
    return inputs[0]


def _SetRunAs(user_file, config_name, c_data, command,
              environment={}, working_directory=""):
  """Add a run_as rule to the user file.

  Arguments:
    user_file: The MSVSUserFile to add the command to.
    config_name: The name of the configuration to add it to
    c_data: The dict of the configuration to add it to
    command: The path to the command to execute.
    args: An array of arguments to the command. (optional)
    working_directory: Directory to run the command in. (optional)
  """
  user_file.AddDebugSettings(_ConfigFullName(config_name, c_data),
                             command, environment, working_directory)


def _AddCustomBuildTool(p, spec, inputs, outputs, description, cmd):
  """Add a custom build tool to execute something.

  Arguments:
    p: the target project
    spec: the target project dict
    inputs: list of inputs
    outputs: list of outputs
    description: description of the action
    cmd: command line to execute
  """
  inputs = [_FixPath(i) for i in inputs]
  outputs = [_FixPath(i) for i in outputs]
  tool = MSVSProject.Tool(
      'VCCustomBuildTool', {
      'Description': description,
      'AdditionalDependencies': ';'.join(inputs),
      'Outputs': ';'.join(outputs),
      'CommandLine': cmd,
      })
  primary_input = _PickPrimaryInput(inputs)
  # Add to the properties of primary input for each config.
  for config_name, c_data in spec['configurations'].iteritems():
    p.AddFileConfig(primary_input,
                    _ConfigFullName(config_name, c_data), tools=[tool])


def _RuleExpandPath(path, input_file):
  """Given the input file to which a rule applied, string substitute a path.

  Arguments:
    path: a path to string expand
    input_file: the file to which the rule applied.
  Returns:
    The string substituted path.
  """
  path = path.replace('$(InputName)',
                      os.path.splitext(os.path.split(input_file)[1])[0])
  path = path.replace('$(InputExt)',
                      os.path.splitext(os.path.split(input_file)[1])[1])
  path = path.replace('$(InputFileName)', os.path.split(input_file)[1])
  path = path.replace('$(InputPath)', input_file)
  return path


def _FindRuleTriggerFiles(rule, sources):
  """Find the list of files which a particular rule applies to.

  Arguments:
    rule: the rule in question
    sources: the set of all known source files for this project
  Returns:
    The list of sources that trigger a particular rule.
  """
  rule_ext = rule['extension']
  return [s for s in sources if s.endswith('.' + rule_ext)]


def _RuleInputsAndOutputs(rule, trigger_file):
  """Find the inputs and outputs generated by a rule.

  Arguments:
    rule: the rule in question
    sources: the set of all known source files for this project
  Returns:
    The pair of (inputs, outputs) involved in this rule.
  """
  raw_inputs = rule.get('inputs', [])
  raw_outputs = rule.get('outputs', [])
  inputs = set()
  outputs = set()
  inputs.add(trigger_file)
  for i in raw_inputs:
    inputs.add(_RuleExpandPath(i, trigger_file))
  for o in raw_outputs:
    outputs.add(_RuleExpandPath(o, trigger_file))
  return (inputs, outputs)


def _GenerateNativeRules(p, rules, output_dir, spec, options):
  """Generate a native rules file.

  Arguments:
    p: the target project
    rules: the set of rules to include
    output_dir: the directory in which the project/gyp resides
    spec: the project dict
    options: global generator options
  """
  rules_filename = '%s%s.rules' % (spec['target_name'],
                                   options.suffix)
  rules_file = MSVSToolFile.Writer(os.path.join(output_dir, rules_filename))
  rules_file.Create(spec['target_name'])
  # Add each rule.
  for r in rules:
    rule_name = r['rule_name']
    rule_ext = r['extension']
    inputs = [_FixPath(i) for i in r.get('inputs', [])]
    outputs = [_FixPath(i) for i in r.get('outputs', [])]
    cmd = _PrepareAction(spec, r, has_input_path=True)
    rules_file.AddCustomBuildRule(name=rule_name,
                                  description=r.get('message', rule_name),
                                  extensions=[rule_ext],
                                  additional_dependencies=inputs,
                                  outputs=outputs,
                                  cmd=cmd)
  # Write out rules file.
  rules_file.Write()

  # Add rules file to project.
  p.AddToolFile(rules_filename)


def _Cygwinify(path):
  path = path.replace('$(OutDir)', '$(OutDirCygwin)')
  path = path.replace('$(IntDir)', '$(IntDirCygwin)')
  return path


def _GenerateExternalRules(p, rules, output_dir, spec,
                           sources, options, actions_to_add):
  """Generate an external makefile to do a set of rules.

  Arguments:
    p: the target project
    rules: the list of rules to include
    output_dir: path containing project and gyp files
    spec: project specification data
    sources: set of sources known
    options: global generator options
  """
  filename = '%s_rules%s.mk' % (spec['target_name'], options.suffix)
  file = gyp.common.WriteOnDiff(os.path.join(output_dir, filename))
  # Find cygwin style versions of some paths.
  file.write('OutDirCygwin:=$(shell cygpath -u "$(OutDir)")\n')
  file.write('IntDirCygwin:=$(shell cygpath -u "$(IntDir)")\n')
  # Gather stuff needed to emit all: target.
  all_inputs = set()
  all_outputs = set()
  all_output_dirs = set()
  first_outputs = []
  for rule in rules:
    trigger_files = _FindRuleTriggerFiles(rule, sources)
    for tf in trigger_files:
      inputs, outputs = _RuleInputsAndOutputs(rule, tf)
      all_inputs.update(set(inputs))
      all_outputs.update(set(outputs))
      # Only use one target from each rule as the dependency for
      # 'all' so we don't try to build each rule multiple times.
      first_outputs.append(list(outputs)[0])
      # Get the unique output directories for this rule.
      output_dirs = [os.path.split(i)[0] for i in outputs]
      for od in output_dirs:
        all_output_dirs.add(od)
  first_outputs_cyg = [_Cygwinify(i) for i in first_outputs]
  # Write out all: target, including mkdir for each output directory.
  file.write('all: %s\n' % ' '.join(first_outputs_cyg))
  for od in all_output_dirs:
    file.write('\tmkdir -p %s\n' % od)
  file.write('\n')
  # Define how each output is generated.
  for rule in rules:
    trigger_files = _FindRuleTriggerFiles(rule, sources)
    for tf in trigger_files:
      # Get all the inputs and outputs for this rule for this trigger file.
      inputs, outputs = _RuleInputsAndOutputs(rule, tf)
      inputs = [_Cygwinify(i) for i in inputs]
      outputs = [_Cygwinify(i) for i in outputs]
      # Prepare the command line for this rule.
      cmd = [_RuleExpandPath(c, tf) for c in rule['action']]
      cmd = ['"%s"' % i for i in cmd]
      cmd = ' '.join(cmd)
      # Add it to the makefile.
      file.write('%s: %s\n' % (' '.join(outputs), ' '.join(inputs)))
      file.write('\t%s\n\n' % cmd)
  # Close up the file.
  file.close()

  # Add makefile to list of sources.
  sources.add(filename)
  # Add a build action to call makefile.
  cmd = ['make',
         'OutDir=$(OutDir)',
         'IntDir=$(IntDir)',
         '-j', '${NUMBER_OF_PROCESSORS_PLUS_1}',
         '-f', filename]
  cmd = _PrepareActionRaw(spec, cmd, True, False, True)
  # TODO(bradnelson): this won't be needed if we have a better way to pick
  #                   the primary input.
  all_inputs = list(all_inputs)
  all_inputs.insert(1, filename)
  actions_to_add.append({
      'inputs': [_FixPath(i) for i in all_inputs],
      'outputs': [_FixPath(i) for i in all_outputs],
      'description': 'Running %s' % cmd,
      'cmd': cmd,
      })


def _EscapeEnvironmentVariableExpansion(s):
  """Escapes any % characters so that Windows-style environment variable
     expansions will leave them alone.
     See http://connect.microsoft.com/VisualStudio/feedback/details/106127/cl-d-name-text-containing-percentage-characters-doesnt-compile
     to understand why we have to do this."""
  s = s.replace('%', '%%')
  return s


quote_replacer_regex = re.compile(r'(\\*)"')
def _EscapeCommandLineArgument(s):
  """Escapes a Windows command-line argument, so that the Win32
     CommandLineToArgv function will turn the escaped result back into the
     original string. See http://msdn.microsoft.com/en-us/library/17w5ykft.aspx
     ("Parsing C++ Command-Line Arguments") to understand why we have to do
     this."""
  def replace(match):
    # For a literal quote, CommandLineToArgv requires an odd number of
    # backslashes preceding it, and it produces half as many literal backslashes
    # (rounded down). So we need to produce 2n+1 backslashes.
    return 2 * match.group(1) + '\\"'
  # Escape all quotes so that they are interpreted literally.
  s = quote_replacer_regex.sub(replace, s)
  # Now add unescaped quotes so that any whitespace is interpreted literally.
  s = '"' + s + '"'
  return s


delimiters_replacer_regex = re.compile(r'(\\*)([,;]+)')
def _EscapeVCProjCommandLineArgListItem(s):
  """The VCProj format stores string lists in a single string using commas and
     semi-colons as separators, which must be quoted if they are to be
     interpreted literally. However, command-line arguments may already have
     quotes, and the VCProj parser is ignorant of the backslash escaping
     convention used by CommandLineToArgv, so the command-line quotes and the
     VCProj quotes may not be the same quotes. So to store a general
     command-line argument in a VCProj list, we need to parse the existing
     quoting according to VCProj's convention and quote any delimiters that are
     not already quoted by that convention. The quotes that we add will also be
     seen by CommandLineToArgv, so if backslashes precede them then we also have
     to escape those backslashes according to the CommandLineToArgv
     convention."""
  def replace(match):
    # For a non-literal quote, CommandLineToArgv requires an even number of
    # backslashes preceding it, and it produces half as many literal
    # backslashes. So we need to produce 2n backslashes.
    return 2 * match.group(1) + '"' + match.group(2) + '"'
  list = s.split('"')
  # The unquoted segments are at the even-numbered indices.
  for i in range(0, len(list), 2):
    list[i] = delimiters_replacer_regex.sub(replace, list[i])
  # Concatenate back into a single string
  s = '"'.join(list)
  if len(list) % 2 == 0:
    # String ends while still quoted according to VCProj's convention. This
    # means the delimiter and the next list item that follow this one in the
    # .vcproj file will be misinterpreted as part of this item. There is nothing
    # we can do about this. Adding an extra quote would correct the problem in
    # the VCProj but cause the same problem on the final command-line. Moving
    # the item to the end of the list does works, but that's only possible if
    # there's only one such item. Let's just warn the user.
    print >> sys.stderr, ('Warning: MSVS may misinterpret the odd number of ' +
        'quotes in ' + s)
  return s


def _EscapeCppDefine(s):
  """Escapes a CPP define so that it will reach the compiler unaltered."""
  s = _EscapeEnvironmentVariableExpansion(s)
  s = _EscapeCommandLineArgument(s)
  s = _EscapeVCProjCommandLineArgListItem(s)
  return s


def _GenerateRules(p, output_dir, options, spec,
                   sources, excluded_sources,
                   actions_to_add):
  """Generate all the rules for a particular project.

  Arguments:
    output_dir: directory to emit rules to
    options: global options passed to the generator
    spec: the specification for this project
    sources: the set of all known source files in this project
    excluded_sources: the set of sources excluded from normal processing
    actions_to_add: deferred list of actions to add in
  """
  rules = spec.get('rules', [])
  rules_native = [r for r in rules if not int(r.get('msvs_external_rule', 0))]
  rules_external = [r for r in rules if int(r.get('msvs_external_rule', 0))]

  # Handle rules that use a native rules file.
  if rules_native:
   _GenerateNativeRules(p, rules_native, output_dir, spec, options)

  # Handle external rules (non-native rules).
  if rules_external:
    _GenerateExternalRules(p, rules_external, output_dir, spec,
                           sources, options, actions_to_add)

  # Add outputs generated by each rule (if applicable).
  for rule in rules:
    # Done if not processing outputs as sources.
    if int(rule.get('process_outputs_as_sources', False)):
      # Add in the outputs from this rule.
      trigger_files = _FindRuleTriggerFiles(rule, sources)
      for tf in trigger_files:
        inputs, outputs = _RuleInputsAndOutputs(rule, tf)
        inputs.remove(tf)
        sources.update(inputs)
        excluded_sources.update(inputs)
        sources.update(outputs)


def _GenerateProject(proj_path, build_file, spec, options, version):
  """Generates a vcproj file.

  Arguments:
    proj_path: Path of the vcproj file to generate.
    build_file: Filename of the .gyp file that the vcproj file comes from.
    spec: The target dictionary containing the properties of the target.
  """
  # Pluck out the default configuration.
  default_config = spec['configurations'][spec['default_configuration']]
  # Decide the guid of the project.
  guid = default_config.get('msvs_guid')
  if guid:
    if VALID_MSVS_GUID_CHARS.match(guid) == None:
      raise ValueError('Invalid MSVS guid: "%s".  Must match regex: "%s".' %
                       (guid, VALID_MSVS_GUID_CHARS.pattern))
    guid = '{%s}' % guid

  # Skip emitting anything if told to with msvs_existing_vcproj option.
  if default_config.get('msvs_existing_vcproj'):
    return guid

  guid = guid or MSVSNew.MakeGuid(proj_path)
  _GenerateMsvsProject(proj_path, guid, build_file, spec, options, version)

  # Return the guid so we can refer to it elsewhere.
  return guid


def _GenerateMsvsProject(proj_path, guid, build_file, spec, options, version):
  """Generates a .vcproj file.  It may create .rules and .user files too.

  Arguments:
    proj_path: The path of the project file to be created.  The .rules and
               .user files will start with the same path.
    guid: The GUID of this project.
    build_file: Filename of the .gyp file that the vcproj file comes from.
    spec: The target dictionary containing the properties of the target.
    options: Global options passed to the generator.
    version: The VisualStudioVersion object.
  """
  vcproj_dir = os.path.dirname(proj_path)
  if vcproj_dir and not os.path.exists(vcproj_dir):
    os.makedirs(vcproj_dir)

  platforms = _GetUniquePlatforms(spec)

  p = MSVSProject.Writer(proj_path, version=version)
  p.Create(spec['target_name'], guid=guid, platforms=platforms)
  user_file = _CreateMsvsUserFile(proj_path, version, spec)

  # Get directory project file is in.
  gyp_dir = os.path.split(proj_path)[0]

  config_type = _GetMsvsConfigurationType(spec)
  for config_name, config in spec['configurations'].iteritems():
    _AddConfigurationToMsvsProject(p, spec, config_type, config_name, config)

  # Prepare list of sources and excluded sources.
  sources, excluded_sources = _PrepareListOfSources(spec, build_file)

  # Add rules.
  actions_to_add = []
  _GenerateRules(p, gyp_dir, options, spec,
                 sources, excluded_sources,
                 actions_to_add)
  sources, excluded_sources, excluded_idl = _AdjustSources(spec, options,
      gyp_dir, sources, excluded_sources)

  # Add in files.
  p.AddFiles(sources)

  # Add deferred actions to add.
  for a in actions_to_add:
    _AddCustomBuildTool(p, spec,
                        inputs=a['inputs'],
                        outputs=a['outputs'],
                        description=a['description'],
                        cmd=a['cmd'])

  _ExcludeFilesFromBeingBuilt(p, spec, excluded_sources, excluded_idl)
  _AddToolFiles(p, spec)
  _HandlePreCompileHeaderStubs(p, spec)
  _AddActions(p, spec)
  has_run_as = _AddRunAs(p, spec, user_file)
  _AddCopies(p, spec)

  # Write it out.
  p.Write()

  # Write out the user file, but only if we need to.
  if has_run_as:
    user_file.Write()


def _GetUniquePlatforms(spec):
  """Return the list of unique platforms for this spec, e.g ['win32', ...]

  Arguments:
    spec: The target dictionary containing the properties of the target.
  Returns:
    The MSVSUserFile object created.
  """
  # Gather list of unique platforms.
  platforms = set()
  for configuration in spec['configurations']:
    platforms.add(_ConfigPlatform(spec['configurations'][configuration]))
  platforms = list(platforms)
  return platforms


def _CreateMsvsUserFile(proj_path, version, spec):
  """Generates a .user file for the user running this Gyp program.

  Arguments:
    proj_path: The path of the project file being created.  The .user file
               shares the same path (with an appropriate suffix).
    version: The VisualStudioVersion object.
    spec: The target dictionary containing the properties of the target.
  Returns:
    The MSVSUserFile object created.
  """
  (domain, username) = _GetDomainAndUserName()
  vcuser_filename = '.'.join([proj_path, domain, username, 'user'])
  user_file = MSVSUserFile.Writer(vcuser_filename, version=version)
  user_file.Create(spec['target_name'])
  return user_file


def _GetMsvsConfigurationType(spec):
  """Returns the configuration type for this project.  It's a number defined
     by Microsoft.  May raise an exception.
  Returns:
    An integer, the configuration type.
  """
  try:
    config_type = {
        'executable': '1',  # .exe
        'shared_library': '2',  # .dll
        'loadable_module': '2',  # .dll
        'static_library': '4',  # .lib
        'none': '10',  # Utility type
        'dummy_executable': '1',  # .exe
        }[spec['type']]
  except KeyError, e:
    if spec.get('type'):
      raise Exception('Target type %s is not a valid target type for '
                      'target %s in %s.' %
                      (spec['type'], spec['target_name'], build_file))
    else:
      raise Exception('Missing type field for target %s in %s.' %
                      (spec['target_name'], build_file))
  return config_type


def _AddConfigurationToMsvsProject(p, spec, config_type, config_name, config):
  """Many settings in a vcproj file are specific to a configuration.  This
    function the main part of the vcproj file that's configuration specific.

  Arguments:
    p: The target project being generated.
    spec: The target dictionary containing the properties of the target.
    config_type: The configuration type, a number as defined by Microsoft.
    config_name: The name of the configuration.
    config: The dictionnary that defines the special processing to be done
            for this configuration.
  """
  # Get the information for this configuration
  include_dirs, resource_include_dirs = _GetIncludeDirs(config)
  libraries = _GetLibraries(config, spec)
  out_file, vc_tool = _GetOutputFilePathAndTool(spec)
  defines = _GetDefines(config)
  disabled_warnings = _GetDisabledWarnings(config)
  prebuild = config.get('msvs_prebuild')
  postbuild = config.get('msvs_postbuild')
  def_file = _GetModuleDefinition(spec)
  precompiled_header = config.get('msvs_precompiled_header')

  # Prepare the list of tools as a dictionary.
  tools = dict()
  # Add in user specified msvs_settings.
  for tool in config.get('msvs_settings', {}):
    settings = config['msvs_settings'][tool]
    for setting in settings:
      _ToolAppend(tools, tool, setting, settings[setting])
  # Add the information to the appropriate tool
  _ToolAppend(tools, 'VCCLCompilerTool',
              'AdditionalIncludeDirectories', include_dirs)
  _ToolAppend(tools, 'VCResourceCompilerTool',
              'AdditionalIncludeDirectories', resource_include_dirs)
  # Add in libraries.
  _ToolAppend(tools, 'VCLinkerTool', 'AdditionalDependencies', libraries)
  if out_file:
    _ToolAppend(tools, vc_tool, 'OutputFile', out_file, only_if_unset=True)
  # Add defines.
  _ToolAppend(tools, 'VCCLCompilerTool', 'PreprocessorDefinitions', defines)
  _ToolAppend(tools, 'VCResourceCompilerTool', 'PreprocessorDefinitions',
              defines)
  # Change program database directory to prevent collisions.
  _ToolAppend(tools, 'VCCLCompilerTool', 'ProgramDataBaseFileName',
              '$(IntDir)\\$(ProjectName)\\vc80.pdb')
  # Add disabled warnings.
  _ToolAppend(tools, 'VCCLCompilerTool',
              'DisableSpecificWarnings', disabled_warnings)
  # Add Pre-build.
  _ToolAppend(tools, 'VCPreBuildEventTool', 'CommandLine', prebuild)
  # Add Post-build.
  _ToolAppend(tools, 'VCPostBuildEventTool', 'CommandLine', postbuild)
  # Turn on precompiled headers if appropriate.
  if precompiled_header:
    precompiled_header = os.path.split(precompiled_header)[1]
    _ToolAppend(tools, 'VCCLCompilerTool', 'UsePrecompiledHeader', '2')
    _ToolAppend(tools, 'VCCLCompilerTool',
                'PrecompiledHeaderThrough', precompiled_header)
    _ToolAppend(tools, 'VCCLCompilerTool',
                'ForcedIncludeFiles', precompiled_header)
  # Loadable modules don't generate import libraries;
  # tell dependent projects to not expect one.
  if spec['type'] == 'loadable_module':
    _ToolAppend(tools, 'VCLinkerTool', 'IgnoreImportLibrary', 'true')
  # Set the module definition file if any.
  if def_file:
      _ToolAppend(tools, 'VCLinkerTool', 'ModuleDefinitionFile', def_file)

  _AddConfiguration(p, tools, config, config_type, config_name)


def _GetIncludeDirs(config):
  """Returns the list of directories to be used for #include directives.

  Arguments:
    config: The dictionnary that defines the special processing to be done
            for this configuration.
  Returns:
    The list of directory paths.
  """
  # TODO(bradnelson): include_dirs should really be flexible enough not to
  #                   require this sort of thing.
  include_dirs = (
      config.get('include_dirs', []) +
      config.get('msvs_system_include_dirs', []))
  resource_include_dirs = config.get('resource_include_dirs', include_dirs)
  include_dirs = [_FixPath(i) for i in include_dirs]
  resource_include_dirs = [_FixPath(i) for i in resource_include_dirs]
  return include_dirs, resource_include_dirs


def _GetLibraries(config, spec):
  """Returns the list of libraries for this configuration.

  Arguments:
    config: The dictionnary that defines the special processing to be done
            for this configuration.
    spec: The target dictionary containing the properties of the target.
  Returns:
    The list of directory paths.
  """
  libraries = spec.get('libraries', [])
  # Strip out -l, as it is not used on windows (but is needed so we can pass
  # in libraries that are assumed to be in the default library path).
  return [re.sub('^(\-l)', '', lib) for lib in libraries]


def _GetOutputFilePathAndTool(spec):
  """Figures out the path of the file this spec will create and the name of
     the VC tool that will create it.

  Arguments:
    spec: The target dictionary containing the properties of the target.
  Returns:
    A pair of (file path, name of the tool)
  """
  # Select a name for the output file.
  out_file = ""
  vc_tool = ""
  output_file_map = {
      'executable': ('VCLinkerTool', '$(OutDir)\\', '.exe'),
      'shared_library': ('VCLinkerTool', '$(OutDir)\\', '.dll'),
      'loadable_module': ('VCLinkerTool', '$(OutDir)\\', '.dll'),
      'static_library': ('VCLibrarianTool', '$(OutDir)\\lib\\', '.lib'),
      'dummy_executable': ('VCLinkerTool', '$(IntDir)\\', '.junk'),
  }
  output_file_props = output_file_map.get(spec['type'])
  if output_file_props and int(spec.get('msvs_auto_output_file', 1)):
    vc_tool, out_dir, suffix = output_file_props
    out_dir = spec.get('product_dir', out_dir)
    product_extension = spec.get('product_extension')
    if product_extension:
      suffix = '.' + product_extension
    prefix = spec.get('product_prefix', '')
    product_name = spec.get('product_name', '$(ProjectName)')
    out_file = ntpath.join(out_dir, prefix + product_name + suffix)
  return out_file, vc_tool


def _GetDefines(config):
  """Returns the list of preprocessor definitions for this configuation.

  Arguments:
    config: The dictionnary that defines the special processing to be done
            for this configuration.
  Returns:
    The list of preprocessor definitions.
  """
  defines = []
  for d in config.get('defines', []):
    if type(d) == list:
      fd = '='.join([str(dpart) for dpart in d])
    else:
      fd = str(d)
    fd = _EscapeCppDefine(fd)
    defines.append(fd)
  return defines


def _GetDisabledWarnings(config):
  return [str(i) for i in config.get('msvs_disabled_warnings', [])]


def _GetModuleDefinition(spec):
  def_file = ""
  if spec['type'] in ['shared_library', 'loadable_module']:
    def_files = [s for s in spec.get('sources', []) if s.endswith('.def')]
    if len(def_files) == 1:
      def_file = _FixPath(def_files[0])
    elif def_files:
      raise ValueError('Multiple module definition files in one target, '
                       'target %s lists multiple .def files: %s' % (
          spec['target_name'], ' '.join(def_files)))
  return def_file


def _ConvertToolsToExpectedForm(tools):
  """ Convert the content of the tools array to a form expected by
      VisualStudio.

  Arguments:
    tools: A dictionnary of settings; the tool name is the key.
  Returns:
    A list of Tool objects.
  """
  tool_list = []
  for tool, settings in tools.iteritems():
    # Collapse settings with lists.
    settings_fixed = {}
    for setting, value in settings.iteritems():
      if type(value) == list:
        if ((tool == 'VCLinkerTool' and
             setting == 'AdditionalDependencies') or
            setting == 'AdditionalOptions'):
          settings_fixed[setting] = ' '.join(value)
        else:
          settings_fixed[setting] = ';'.join(value)
      else:
        settings_fixed[setting] = value
    # Add in this tool.
    tool_list.append(MSVSProject.Tool(tool, settings_fixed))
  return tool_list


def _AddConfiguration(p, tools, config, config_type, config_name):
  """Add to the project file the configuration specified by config.

  Arguments:
    p: The target project being generated.
    tools: A dictionnary of settings; the tool name is the key.
    config: The dictionnary that defines the special processing to be done
            for this configuration.
    config_type: The configuration type, a number as defined by Microsoft.
    config_name: The name of the configuration.
  """
  # Prepare configuration attributes.
  prepared_attrs = {}
  source_attrs = config.get('msvs_configuration_attributes', {})
  for a in source_attrs:
    prepared_attrs[a] = source_attrs[a]
  # Add props files.
  vsprops_dirs = config.get('msvs_props', [])
  vsprops_dirs = [_FixPath(i) for i in vsprops_dirs]
  if vsprops_dirs:
    prepared_attrs['InheritedPropertySheets'] = ';'.join(vsprops_dirs)
  # Set configuration type.
  prepared_attrs['ConfigurationType'] = config_type
  if not prepared_attrs.has_key('OutputDirectory'):
    prepared_attrs['OutputDirectory'] = '$(SolutionDir)$(ConfigurationName)'
  if not prepared_attrs.has_key('IntermediateDirectory'):
    intermediate = '$(ConfigurationName)\\obj\\$(ProjectName)'
    prepared_attrs['IntermediateDirectory'] = intermediate

  # Add in this configuration.
  tool_list = _ConvertToolsToExpectedForm(tools)
  p.AddConfig(_ConfigFullName(config_name, config),
              attrs=prepared_attrs, tools=tool_list)


def _PrepareListOfSources(spec, build_file):
  """Prepare list of sources and excluded sources. Besides the sources
     specified directly in the spec, adds the gyp file so that a change
     to it will cause a re-compile.  Also adds appropriate sources for
     actions and copies.

  Arguments:
    spec: The target dictionary containing the properties of the target.
    build_file: Filename of the .gyp file that the vcproj file comes from.
  Returns:
    A pair of (list of sources, list of excluded sources)
  """
  sources = set(spec.get('sources', []))
  excluded_sources = set()
  # Add in the gyp file.
  gyp_file = os.path.split(build_file)[1]
  sources.add(gyp_file)
  # Add in 'action' inputs and outputs.
  for a in spec.get('actions', []):
    inputs = a.get('inputs')
    if not inputs:
      # This is an action with no inputs.  Make the primary input
      # be the .gyp file itself so Visual Studio has a place to
      # hang the custom build rule.
      inputs = [gyp_file]
      a['inputs'] = inputs
    primary_input = _PickPrimaryInput(inputs)
    inputs = set(inputs)
    sources.update(inputs)
    inputs.remove(primary_input)
    excluded_sources.update(inputs)
    if int(a.get('process_outputs_as_sources', False)):
      outputs = set(a.get('outputs', []))
      sources.update(outputs)
  # Add in 'copies' inputs and outputs.
  for cpy in spec.get('copies', []):
    files = set(cpy.get('files', []))
    sources.update(files)
  return (sources, excluded_sources)


def _AdjustSources(spec, options, gyp_dir, sources, excluded_sources):
  """Adjusts the list of sources and excluded sources.
     Also converts the sets to lists.

  Arguments:
    spec: The target dictionary containing the properties of the target.
    options: Global generator options.
    gyp_dir: The path to the gyp file being processed.
    sources: A set of sources to be included for this project.
    sources: A set of sources to be excluded for this project.
  Returns:
    A trio of (list of sources, list of excluded sources,
               path of excluded IDL file)
  """
  # Exclude excluded sources coming into the generator.
  excluded_sources.update(set(spec.get('sources_excluded', [])))
  # Add excluded sources into sources for good measure.
  sources.update(excluded_sources)
  # Convert to proper windows form.
  # NOTE: sources goes from being a set to a list here.
  # NOTE: excluded_sources goes from being a set to a list here.
  sources = [_FixPath(i) for i in sources]
  # Convert to proper windows form.
  excluded_sources = [_FixPath(i) for i in excluded_sources]

  excluded_idl = _IdlFilesHandledNonNatively(spec, sources)

  precompiled_related = _GetPrecompileRelatedFiles(spec)
  # Find the excluded ones, minus the precompiled header related ones.
  fully_excluded = [i for i in excluded_sources if i not in precompiled_related]

  # Convert to folders and the right slashes.
  sources = [i.split('\\') for i in sources]
  sources = _SourceInFolders(sources, excluded=fully_excluded)
  # Add in dummy file for type none.
  if spec['type'] == 'dummy_executable':
    # Pull in a dummy main so it can link successfully.
    dummy_relpath = gyp.common.RelativePath(
        options.depth + '\\tools\\gyp\\gyp_dummy.c', gyp_dir)
    sources.append(dummy_relpath)

  return sources, excluded_sources, excluded_idl


def _IdlFilesHandledNonNatively(spec, sources):
  # If any non-native rules use 'idl' as an extension exclude idl files.
  # Gather a list here to use later.
  using_idl = False
  for rule in spec.get('rules', []):
    if rule['extension'] == 'idl' and int(rule.get('msvs_external_rule', 0)):
      using_idl = True
      break
  if using_idl:
    excluded_idl = [i for i in sources if i.endswith('.idl')]
  else:
    excluded_idl = []
  return excluded_idl


def _GetPrecompileRelatedFiles(spec):
  # Gather a list of precompiled header related sources.
  precompiled_related = []
  for config_name, config in spec['configurations'].iteritems():
    for k in precomp_keys:
      f = config.get(k)
      if f:
        precompiled_related.append(_FixPath(f))
  return precompiled_related


def _ExcludeFilesFromBeingBuilt(p, spec, excluded_sources, excluded_idl):
  # Exclude excluded sources from being built.
  for f in excluded_sources:
    for config_name, config in spec['configurations'].iteritems():
      precomped = [_FixPath(config.get(i, '')) for i in precomp_keys]
      # Don't do this for ones that are precompiled header related.
      if f not in precomped:
        p.AddFileConfig(f, _ConfigFullName(config_name, config),
                        {'ExcludedFromBuild': 'true'})

  # If any non-native rules use 'idl' as an extension exclude idl files.
  # Exclude them now.
  for config_name, config in spec['configurations'].iteritems():
    for f in excluded_idl:
      p.AddFileConfig(f, _ConfigFullName(config_name, config),
                      {'ExcludedFromBuild': 'true'})


def _AddToolFiles(p, spec):
  # Add in tool files (rules).
  tool_files = set()
  for config_name, config in spec['configurations'].iteritems():
    for f in config.get('msvs_tool_files', []):
      tool_files.add(f)
  for f in tool_files:
    p.AddToolFile(f)


def _HandlePreCompileHeaderStubs(p, spec):
  # Handle pre-compiled headers source stubs specially.
  for config_name, config in spec['configurations'].iteritems():
    source = config.get('msvs_precompiled_source')
    if source:
      source = _FixPath(source)
      # UsePrecompiledHeader=1 for if using precompiled headers.
      tool = MSVSProject.Tool('VCCLCompilerTool',
                              {'UsePrecompiledHeader': '1'})
      p.AddFileConfig(source, _ConfigFullName(config_name, config),
                      {}, tools=[tool])


def _AddActions(p, spec):
  # Add actions.
  actions = spec.get('actions', [])
  for a in actions:
    cmd = _PrepareAction(spec, a, has_input_path=False)
    _AddCustomBuildTool(p, spec,
                        inputs=a.get('inputs', []),
                        outputs=a.get('outputs', []),
                        description=a.get('message', a['action_name']),
                        cmd=cmd)


def _AddRunAs(p, spec, user_file):
  # Add run_as and test targets.
  has_run_as = False
  if spec.get('run_as') or int(spec.get('test', 0)):
    has_run_as = True
    run_as = spec.get('run_as', {
      'action' : ['$(TargetPath)', '--gtest_print_time'],
      })
    working_directory = run_as.get('working_directory', '.')
    action = run_as.get('action', [])
    environment = run_as.get('environment', [])
    for config_name, c_data in spec['configurations'].iteritems():
      _SetRunAs(user_file, config_name, c_data,
                action, environment, working_directory)
  return has_run_as


def _AddCopies(p, spec):
  # Add copies.
  for cpy in spec.get('copies', []):
    for src in cpy.get('files', []):
      dst = os.path.join(cpy['destination'], os.path.basename(src))
      # _AddCustomBuildTool() will call _FixPath() on the inputs and
      # outputs, so do the same for our generated command line.
      if src.endswith('/'):
        src_bare = src[:-1]
        base_dir = posixpath.split(src_bare)[0]
        outer_dir = posixpath.split(src_bare)[1]
        cmd = 'cd "%s" && xcopy /e /f /y "%s" "%s\\%s\\"' % (
            _FixPath(base_dir), outer_dir, _FixPath(dst), outer_dir)
        _AddCustomBuildTool(p, spec,
                            inputs=[src],
                            outputs=['dummy_copies', dst],
                            description='Copying %s to %s' % (src, dst),
                            cmd=cmd)
      else:
        cmd = 'mkdir "%s" 2>nul & set ERRORLEVEL=0 & copy /Y "%s" "%s"' % (
            _FixPath(cpy['destination']), _FixPath(src), _FixPath(dst))
        _AddCustomBuildTool(p, spec,
                            inputs=[src], outputs=[dst],
                            description='Copying %s to %s' % (src, dst),
                            cmd=cmd)


def _GetPathDict(root, path):
  if path == '':
    return root
  parent, folder = os.path.split(path)
  parent_dict = _GetPathDict(root, parent)
  if folder not in parent_dict:
    parent_dict[folder] = dict()
  return parent_dict[folder]


def _DictsToFolders(base_path, bucket, flat):
  # Convert to folders recursively.
  children = []
  for folder, contents in bucket.iteritems():
    if type(contents) == dict:
      folder_children = _DictsToFolders(os.path.join(base_path, folder),
                                        contents, flat)
      if flat:
        children += folder_children
      else:
        folder_children = MSVSNew.MSVSFolder(os.path.join(base_path, folder),
                                             name='(' + folder + ')',
                                             entries=folder_children)
        children.append(folder_children)
    else:
      children.append(contents)
  return children


def _CollapseSingles(parent, node):
  # Recursively explorer the tree of dicts looking for projects which are
  # the sole item in a folder which has the same name as the project. Bring
  # such projects up one level.
  if (type(node) == dict and
      len(node) == 1 and
      node.keys()[0] == parent + '.vcproj'):
    return node[node.keys()[0]]
  if type(node) != dict:
    return node
  for child in node.keys():
    node[child] = _CollapseSingles(child, node[child])
  return node


def _GatherSolutionFolders(project_objs, flat):
  root = {}
  # Convert into a tree of dicts on path.
  for p in project_objs.keys():
    gyp_file, target = gyp.common.ParseQualifiedTarget(p)[0:2]
    gyp_dir = os.path.dirname(gyp_file)
    path_dict = _GetPathDict(root, gyp_dir)
    path_dict[target + '.vcproj'] = project_objs[p]
  # Walk down from the top until we hit a folder that has more than one entry.
  # In practice, this strips the top-level "src/" dir from the hierarchy in
  # the solution.
  while len(root) == 1 and type(root[root.keys()[0]]) == dict:
    root = root[root.keys()[0]]
  # Collapse singles.
  root = _CollapseSingles('', root)
  # Merge buckets until everything is a root entry.
  return _DictsToFolders('', root, flat)


def _ProjectObject(sln, qualified_target, project_objs, projects):
  # Done if this project has an object.
  if project_objs.get(qualified_target):
    return project_objs[qualified_target]
  # Get dependencies for this project.
  spec = projects[qualified_target]['spec']
  deps = spec.get('dependencies', [])
  # Get objects for each dependency.
  deps = [_ProjectObject(sln, d, project_objs, projects) for d in deps]
  # Find relative path to vcproj from sln.
  vcproj_rel_path = gyp.common.RelativePath(
      projects[qualified_target]['vcproj_path'], os.path.split(sln)[0])
  vcproj_rel_path = _FixPath(vcproj_rel_path)
  # Prepare a dict indicating which project configurations are used for which
  # solution configurations for this target.
  config_platform_overrides = {}
  for config_name, c in spec['configurations'].iteritems():
    config_fullname = _ConfigFullName(config_name, c)
    platform = c.get('msvs_target_platform', _ConfigPlatform(c))
    fixed_config_fullname = '%s|%s' % (
        _ConfigBaseName(config_name, _ConfigPlatform(c)), platform)
    config_platform_overrides[config_fullname] = fixed_config_fullname
  # Create object for this project.
  obj = MSVSNew.MSVSProject(
      vcproj_rel_path,
      name=spec['target_name'],
      guid=projects[qualified_target]['guid'],
      dependencies=deps,
      config_platform_overrides=config_platform_overrides)
  # Store it to the list of objects.
  project_objs[qualified_target] = obj
  # Return project object.
  return obj


def CalculateVariables(default_variables, params):
  """Generated variables that require params to be known."""

  generator_flags = params.get('generator_flags', {})

  # Select project file format version (if unset, default to auto detecting).
  msvs_version = \
    MSVSVersion.SelectVisualStudioVersion(generator_flags.get('msvs_version',
                                                              'auto'))
  # Stash msvs_version for later (so we don't have to probe the system twice).
  params['msvs_version'] = msvs_version

  # Set a variable so conditions can be based on msvs_version.
  default_variables['MSVS_VERSION'] = msvs_version.ShortName()

  # To determine processor word size on Windows, in addition to checking
  # PROCESSOR_ARCHITECTURE (which reflects the word size of the current
  # process), it is also necessary to check PROCESSOR_ARCITEW6432 (which
  # contains the actual word size of the system when running thru WOW64).
  if (os.environ.get('PROCESSOR_ARCHITECTURE', '').find('64') >= 0 or
      os.environ.get('PROCESSOR_ARCHITEW6432', '').find('64') >= 0):
    default_variables['MSVS_OS_BITS'] = 64
  else:
    default_variables['MSVS_OS_BITS'] = 32


def GenerateOutput(target_list, target_dicts, data, params):
  """Generate .sln and .vcproj files.

  This is the entry point for this generator.
  Arguments:
    target_list: List of target pairs: 'base/base.gyp:base'.
    target_dicts: Dict of target properties keyed on target pair.
    data: Dictionary containing per .gyp data.
  """
  global fixpath_prefix

  options = params['options']
  generator_flags = params.get('generator_flags', {})

  # Get the project file format version back out of where we stashed it in
  # GeneratorCalculatedVariables.
  msvs_version = params['msvs_version']

  # Prepare the set of configurations.
  configs = set()
  for qualified_target in target_list:
    build_file = gyp.common.BuildFile(qualified_target)
    spec = target_dicts[qualified_target]
    for config_name, config in spec['configurations'].iteritems():
      configs.add(_ConfigFullName(config_name, config))
  configs = list(configs)

  # Generate each project.
  projects = {}
  for qualified_target in target_list:
    build_file = gyp.common.BuildFile(qualified_target)
    spec = target_dicts[qualified_target]
    if spec['toolset'] != 'target':
      raise Exception(
          'Multiple toolsets not supported in msvs build (target %s)' %
          qualified_target)
    default_config = spec['configurations'][spec['default_configuration']]
    proj_filename = default_config.get('msvs_existing_vcproj')
    if not proj_filename:
      proj_filename = spec['target_name'] + options.suffix + '.vcproj'
    proj_path = os.path.join(os.path.split(build_file)[0], proj_filename)
    if options.generator_output:
      projectDirPath = os.path.dirname(os.path.abspath(proj_path))
      proj_path = os.path.join(options.generator_output, proj_path)
      fixpath_prefix = gyp.common.RelativePath(projectDirPath,
                                               os.path.dirname(proj_path))
    projects[qualified_target] = {
        'vcproj_path': proj_path,
        'guid': _GenerateProject(proj_path, build_file,
                                 spec, options, version=msvs_version),
        'spec': spec,
    }

  fixpath_prefix = None

  for build_file in data.keys():
    # Validate build_file extension
    if build_file[-4:] != '.gyp':
      continue
    sln_path = build_file[:-4] + options.suffix + '.sln'
    if options.generator_output:
      sln_path = os.path.join(options.generator_output, sln_path)
    # Get projects in the solution, and their dependents.
    sln_projects = gyp.common.BuildFileTargets(target_list, build_file)
    sln_projects += gyp.common.DeepDependencyTargets(target_dicts, sln_projects)
    # Convert projects to Project Objects.
    project_objs = {}
    for p in sln_projects:
      _ProjectObject(sln_path, p, project_objs, projects)
    # Create folder hierarchy.
    root_entries = _GatherSolutionFolders(
        project_objs, flat=msvs_version.FlatSolution())
    # Create solution.
    sln = MSVSNew.MSVSSolution(sln_path,
                               entries=root_entries,
                               variants=configs,
                               websiteProperties=False,
                               version=msvs_version)
    sln.Write()
