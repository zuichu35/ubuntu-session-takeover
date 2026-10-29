"""Read-only candidate inspection. No dlopen, ldd, Shell execution, bus or service calls.

Structural checks and a trusted target-test receipt are separate requirements.
A matching hash alone is never evidence of working compositor behaviour.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import stat
import struct

DEFAULT_PROFILE = 'ubuntu-26.04-amd64-gnome-50.1'
MANIFEST = 'build/compatibility-manifest.json'
VALIDATION = 'build/compatibility-validation.json'
PROFILE_RE = re.compile(r'[a-z0-9][a-z0-9.-]{0,100}\Z')
SHA_RE = re.compile(r'[0-9a-f]{64}\Z')


class CompatibilityError(RuntimeError):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _within(path, anchor):
    return path == anchor or path.is_relative_to(anchor)


def trusted_read(path, anchor, required_owner=0):
    """Check both symlink and resolved ancestry before reading a regular file.

    Production requires root-owned, non-writable ancestry all the way to /.
    Explicit developer checks also accept that UID's group-writable build
    tree and root's sticky /tmp. Such reports cannot authorize production.
    """
    path, anchor = Path(path).absolute(), Path(anchor).absolute()
    resolved = path.resolve(strict=True)
    if not _within(resolved, anchor.resolve(strict=True)):
        raise CompatibilityError('Path escapes its trusted root: ' + str(path))
    for candidate in {path, resolved}:
        for part in [candidate, *candidate.parents]:
            info = part.lstat()
            if info.st_uid not in ({0} if required_owner == 0 else {0, required_owner}):
                raise CompatibilityError('Unexpected path owner: ' + str(part))
            if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
                developer_tmp = (required_owner != 0 and stat.S_ISDIR(info.st_mode)
                                 and info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
                developer_group = (required_owner != 0 and info.st_uid == required_owner
                                   and not info.st_mode & 0o002)
                if not (developer_tmp or developer_group):
                    raise CompatibilityError('Writable trusted path: ' + str(part))
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 128 * 1024 * 1024:
        raise CompatibilityError('Expected a bounded regular file: ' + str(path))
    return resolved.read_bytes()


def elf_metadata(raw):
    """Parse ELF64 little-endian dynamic metadata without executing a loader."""
    def unpack(fmt, offset):
        if offset < 0 or offset + struct.calcsize(fmt) > len(raw):
            raise CompatibilityError('Truncated ELF metadata')
        return struct.unpack_from(fmt, raw, offset)

    if len(raw) < 64 or raw[:6] != b'\x7fELF\x02\x01':
        raise CompatibilityError('Expected ELF64 little-endian binary')
    machine = unpack('<H', 18)[0]
    phoff, shoff = unpack('<QQ', 32)
    phsize, phnum, shsize, shnum = unpack('<HHHH', 54)
    if phnum > 4096 or shnum > 65535 or phsize != 56:
        raise CompatibilityError('Unsupported ELF header table')
    loads, dynamic = [], []
    for index in range(phnum):
        kind, flags, offset, vaddr, _, size, _, _ = unpack('<IIQQQQQQ', phoff + index * phsize)
        if offset + size > len(raw):
            raise CompatibilityError('ELF segment extends beyond file')
        if kind == 1:
            loads.append((vaddr, offset, size))
        if kind == 2:
            if size % 16:
                raise CompatibilityError('Invalid ELF dynamic table')
            dynamic = [unpack('<qQ', pos) for pos in range(offset, offset + size, 16)]
    tags = {}
    for tag, value in dynamic:
        if tag == 0:
            break
        tags.setdefault(tag, []).append(value)

    def cstring(table, offset):
        if not 0 <= offset < len(table):
            raise CompatibilityError('Invalid ELF string offset')
        end = table.find(b'\0', offset)
        if end < 0:
            raise CompatibilityError('Unterminated ELF string')
        return table[offset:end].decode('utf-8', errors='strict')

    table = b''
    if 5 in tags:
        address = tags[5][0]
        length = tags.get(10, [0])[0]
        for vaddr, offset, size in loads:
            if vaddr <= address and address - vaddr + length <= size:
                start = offset + address - vaddr
                table = raw[start:start + length]
                break
        if not table:
            raise CompatibilityError('Invalid ELF dynamic string table')
    exports = set()
    if shnum:
        if shsize != 64:
            raise CompatibilityError('Unsupported ELF section table')
        sections = [unpack('<IIQQQQIIQQ', shoff + i * shsize) for i in range(shnum)]
        for section in sections:
            if section[1] != 11:  # SHT_DYNSYM
                continue
            offset, size, link, entry = section[4], section[5], section[6], section[9]
            if entry != 24 or link >= shnum or size % entry or offset + size > len(raw):
                raise CompatibilityError('Invalid dynamic symbol table')
            strings = sections[link]
            if strings[4] + strings[5] > len(raw):
                raise CompatibilityError('Invalid dynamic symbol string table')
            names = raw[strings[4]:strings[4] + strings[5]]
            for pos in range(offset, offset + size, entry):
                name, info, _, index, _, _ = unpack('<IBBHQQ', pos)
                if name and index and info >> 4 in (1, 2):
                    exports.add(cstring(names, name))
    return {'machine': machine, 'needed': [cstring(table, x) for x in tags.get(1, [])],
            'soname': cstring(table, tags[14][0]) if 14 in tags else None,
            'runpaths': [cstring(table, x) for tag in (15, 29) for x in tags.get(tag, [])],
            'exports': sorted(exports)}


def runtime_paths(root, profile):
    root = Path(root).absolute()
    native = root / profile['paths']['native_prefix']
    grd = root / profile['paths']['grd_prefix']
    library = native / profile['paths']['library_subdir']
    types = library / profile['paths']['mutter_subdir']
    return {name: str(path) for name, path in {
        'runtime_root': root, 'native_prefix': native, 'grd_prefix': grd,
        'library_dir': library, 'mutter_library_dir': types, 'typelib_dir': types,
        'auth_helper': native / 'libexec/native-local-auth',
        'grd_daemon': grd / 'libexec/gnome-remote-desktop-daemon',
        'schema_dir': grd / 'share/glib-2.0/schemas',
    }.items()}


def load_profile(root, profile_id=DEFAULT_PROFILE, required_owner=0):
    if not isinstance(profile_id, str) or not PROFILE_RE.fullmatch(profile_id):
        raise CompatibilityError('Invalid compatibility profile identifier')
    raw = trusted_read(Path(root) / 'compat/profiles' / (profile_id + '.json'), root, required_owner)
    profile = json.loads(raw)
    if profile.get('schema') != 1 or profile.get('id') != profile_id:
        raise CompatibilityError('Invalid compatibility profile')
    for relative in [*profile['paths'].values(), *(x['path'] for x in profile['artifacts'])]:
        if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise CompatibilityError('Unsafe profile relative path')
    return profile, sha256(raw)


def _package_versions(text, requested):
    result = {}
    for paragraph in text.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        if fields.get('Package') in requested and fields.get('Status') == 'install ok installed':
            if fields.get('Architecture') not in ('amd64', 'all'):
                continue
            result[fields['Package']] = fields.get('Version', '')
    return result


def collect_snapshot(root, profile, *, system_root=Path('/'), required_owner=0,
                     profile_sha256='', runtime_prefix=None):
    """Collect serializable facts; optional roots/owner are explicit fixture/build inputs."""
    root, system_root = Path(root).absolute(), Path(system_root).absolute()
    facts = {'paths': runtime_paths(root, profile), 'profile_sha256': profile_sha256,
             'expected_runtime_prefix': str(runtime_prefix or root),
             'staged': runtime_prefix is not None and str(runtime_prefix) != str(root),
             'production_eligible': required_owner == 0, 'errors': [], 'diagnostics': [],
             'artifacts': {}, 'elf': {}, 'system_hashes': {}, 'typelibs': {},
             'manifest': None, 'manifest_sha256': None, 'receipt': None}

    def read(path, anchor):
        return trusted_read(path, anchor, required_owner)

    def optional_json(relative):
        path = root / relative
        if not path.exists() and not path.is_symlink():
            return None, None
        raw = read(path, root)
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise CompatibilityError('Expected JSON object: ' + relative)
        return document, sha256(raw)

    try:
        facts['manifest'], facts['manifest_sha256'] = optional_json(MANIFEST)
        facts['receipt'], _ = optional_json(VALIDATION)
        os_release = read(system_root / 'etc/os-release', system_root).decode()
        release = {}
        for line in os_release.splitlines():
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                release[key] = shlex.split(value)[0] if value else ''
        packages = read(system_root / 'var/lib/dpkg/status', system_root).decode()
        facts['target'] = {'os_id': release.get('ID'), 'os_version': release.get('VERSION_ID'),
                           'architecture': platform.machine(), 'mutter_abi': None,
                           'packages': _package_versions(packages, profile['target']['packages'])}
        for absolute in profile['system_files']:
            raw = read(system_root / absolute.lstrip('/'), system_root)
            facts['system_hashes'][absolute] = sha256(raw)
            if absolute == profile['shell_binary']:
                facts['shell_elf'] = elf_metadata(raw)
                matches = [re.fullmatch(r'libmutter-(\d+)\.so\.0', name)
                           for name in facts['shell_elf']['needed']]
                abis = [match.group(1) for match in matches if match]
                if len(abis) != 1:
                    raise CompatibilityError('Shell does not declare exactly one Mutter SONAME ABI')
                facts['target']['mutter_abi'] = abis[0]
        compiled_root = str(runtime_prefix or (facts['manifest'] or {}).get('runtime_prefix') or root)
        for artifact in profile['artifacts']:
            relative = artifact['path']
            path = root / relative
            raw = read(path, root)
            if not raw:
                raise CompatibilityError('Empty runtime resource: ' + relative)
            facts['artifacts'][relative] = sha256(raw)
            kind = artifact['kind']
            if kind == 'elf':
                metadata = elf_metadata(raw)
                facts['elf'][relative] = metadata
                if artifact.get('executable') and not path.stat().st_mode & 0o111:
                    raise CompatibilityError('Runtime executable lacks execute mode: ' + relative)
                for marker in artifact.get('markers', []):
                    if marker.encode() not in raw:
                        raise CompatibilityError('Missing takeover capability marker: ' + relative)
                for suffix in artifact.get('runtime_paths', []):
                    if (compiled_root.rstrip('/') + '/' + suffix).encode() + b'\0' not in raw:
                        raise CompatibilityError('Compiled resource prefix mismatch: ' + relative)
            elif kind == 'typelib':
                facts['typelibs'][relative] = raw.count(artifact['library'].encode() + b'\0') == 1
    except (OSError, ValueError, KeyError, TypeError, CompatibilityError) as error:
        facts['errors'].append(str(error))
    return facts


def _evaluate(profile, facts):
    """Pure decision report. Runtime integrity failures differ from missing validation."""
    reasons = list(facts.get('errors', []))
    pending, diagnostics = [], list(facts.get('diagnostics', []))
    target = facts.get('target', {})
    expected = profile['target']
    for key in ('os_id', 'os_version', 'architecture', 'mutter_abi'):
        if target.get(key) != expected[key]:
            reasons.append('Unsupported target ' + key + ': ' + str(target.get(key)))
    if target.get('packages') != expected['packages']:
        pending.append('Exact target package tuple needs a separately validated profile')
    shell = facts.get('shell_elf', {})
    if shell.get('machine') != profile['elf_machine']:
        reasons.append('Shell ELF architecture differs from the profile')
    if not set(profile['shell_needed']).issubset(shell.get('needed', [])):
        reasons.append('Shell linkage does not match the explicit profile SONAMEs')
    for artifact in profile['artifacts']:
        name = artifact['path']
        if name not in facts.get('artifacts', {}):
            reasons.append('Missing inspected runtime artifact: ' + name)
            continue
        if artifact['kind'] == 'elf':
            elf = facts.get('elf', {}).get(name, {})
            if elf.get('machine') != profile['elf_machine']:
                reasons.append('Runtime ELF architecture mismatch: ' + name)
            if artifact.get('soname') and elf.get('soname') != artifact['soname']:
                reasons.append('Runtime SONAME mismatch: ' + name)
            if not set(artifact.get('exports', [])).issubset(elf.get('exports', [])):
                reasons.append('Required exported symbols absent: ' + name)
        elif artifact['kind'] == 'typelib' and facts.get('typelibs', {}).get(name) is not True:
            reasons.append('Typelib library reference mismatch: ' + name)
    manifest = facts.get('manifest')
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        reasons.append('Missing or invalid build compatibility manifest')
    else:
        if manifest.get('profile_id') != profile['id'] or manifest.get('profile_sha256') != facts.get('profile_sha256'):
            reasons.append('Build manifest does not match the trusted profile')
        if manifest.get('runtime_prefix') != facts.get('expected_runtime_prefix', facts.get('paths', {}).get('runtime_root')):
            reasons.append('Runtime moved away from its compiled resource prefix')
        if manifest.get('sources') != profile['sources']:
            reasons.append('Build source/distro patch baseline differs from profile')
        recorded = manifest.get('artifacts')
        if not isinstance(recorded, dict) or recorded != facts.get('artifacts') or not recorded:
            reasons.append('Runtime files differ from the build manifest')
        if manifest.get('built_for') != target:
            pending.append('Installed target differs from the build target')
        for name, old in manifest.get('system_hashes', {}).items():
            if facts.get('system_hashes', {}).get(name) != old:
                diagnostics.append('System fingerprint changed (diagnostic only): ' + name)
    receipt = facts.get('receipt')
    if not isinstance(receipt, dict) or receipt.get('schema') != 1:
        pending.append('No target-Shell validation receipt')
    elif (receipt.get('scope') != 'target-shell-integration' or receipt.get('profile_id') != profile['id']
          or receipt.get('manifest_sha256') != facts.get('manifest_sha256')):
        pending.append('Validation receipt is not bound to this candidate and target test scope')
    elif receipt.get('target') != target:
        pending.append('Validation receipt covers another exact target')
    elif not all(receipt.get('cases', {}).get(case) == 'passed' for case in profile['required_cases']):
        pending.append('Required target-Shell integration cases are incomplete')
    else:
        tested = receipt.get('tested_binaries', {})
        expected_hashes = {**{'runtime:' + name: value for name, value in (manifest or {}).get('artifacts', {}).items()
                              if name in profile['tested_runtime']},
                           **{'system:' + name: (manifest or {}).get('system_hashes', {}).get(name)
                              for name in profile['tested_system']}}
        if not expected_hashes or any(tested.get(name) != digest for name, digest in expected_hashes.items()):
            pending.append('Receipt lacks the actual tested Shell/runtime binary hashes')
    return {'schema': 1, 'status': 'incompatible' if reasons else 'needs-validation' if pending else 'verified',
            'structural_ok': not reasons, 'production_eligible': facts.get('production_eligible', False),
            'staged': facts.get('staged', False),
            'profile_id': profile['id'], 'reasons': reasons + pending, 'diagnostics': diagnostics,
            'paths': facts.get('paths', {}), 'target': target,
            'limitations': profile.get('limitations', [])}


def evaluate(profile, facts):
    try:
        return _evaluate(profile, facts)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return {'schema': 1, 'status': 'incompatible', 'structural_ok': False,
                'production_eligible': False, 'profile_id': profile.get('id'),
                'reasons': ['Malformed compatibility evidence: ' + str(error)], 'diagnostics': [],
                'paths': {}, 'target': {}, 'limitations': []}


def inspect_candidate(root, *, required_owner=0, system_root=Path('/'), profile_id=None, runtime_prefix=None):
    try:
        if profile_id is None:
            path = Path(root) / MANIFEST
            if path.exists() or path.is_symlink():
                document = json.loads(trusted_read(path, root, required_owner))
                profile_id = document.get('profile_id')
            else:
                profile_id = DEFAULT_PROFILE
        profile, digest = load_profile(root, profile_id, required_owner)
        return evaluate(profile, collect_snapshot(root, profile, system_root=system_root,
                                                  required_owner=required_owner, profile_sha256=digest,
                                                  runtime_prefix=runtime_prefix))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, CompatibilityError) as error:
        return {'schema': 1, 'status': 'incompatible', 'structural_ok': False,
                'production_eligible': False, 'profile_id': profile_id, 'reasons': [str(error)],
                'diagnostics': [], 'paths': {}, 'target': {}, 'limitations': []}


def private_environment(report):
    if report.get('status') != 'verified' or not report.get('production_eligible') or report.get('staged'):
        raise CompatibilityError('Private environment requires a verified production candidate')
    paths = report['paths']
    return {'LD_LIBRARY_PATH': paths['library_dir'] + ':' + paths['mutter_library_dir'],
            'GI_TYPELIB_PATH': paths['typelib_dir'], 'MUTTER_NATIVE_TYPELIB_DIR': paths['typelib_dir'],
            'MUTTER_NATIVE_LOCAL_AUTH_HELPER': paths['auth_helper']}


def build_manifest(profile, facts, *, runtime_prefix, sources):
    """Return a build receipt, never a test/compatibility approval.

    A staged tree may differ from runtime_prefix. Its tests must load binaries
    from the final compiled prefix before generating a validation receipt.
    """
    if facts.get('errors') or not facts.get('target'):
        raise CompatibilityError('Cannot record incomplete inspection: ' + '; '.join(facts.get('errors', [])))
    expected = {item['path'] for item in profile['artifacts']}
    if set(facts.get('artifacts', {})) != expected or sources != profile['sources']:
        raise CompatibilityError('Build artifacts or source baseline do not match the profile')
    if not Path(runtime_prefix).is_absolute():
        raise CompatibilityError('Compiled runtime prefix must be absolute')
    return json.loads(json.dumps({'schema': 1, 'profile_id': profile['id'],
                                  'profile_sha256': facts['profile_sha256'],
                                  'runtime_prefix': str(runtime_prefix), 'built_for': facts['target'],
                                  'artifacts': facts['artifacts'], 'system_hashes': facts['system_hashes'],
                                  'sources': sources}))
