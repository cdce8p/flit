from configparser import ConfigParser
from collections import defaultdict
import io
import os
from pathlib import Path
from posixpath import join as pjoin
from pprint import pformat
import re
import sys
import tarfile

from flit import common, inifile
from flit.common import VCSError
from flit.vcs import identify_vcs
from flit.wheel import EntryPointsConflict

SETUP = """\
#!/usr/bin/env python

from distutils.core import setup

{before}
setup(name={name!r},
      version={version!r},
      description={description!r},
      author={author!r},
      author_email={author_email!r},
      url={url!r},
      {extra}
     )
"""

PKG_INFO = """\
Metadata-Version: 1.1
Name: {name}
Version: {version}
Summary: {summary}
Home-page: {home_page}
Author: {author}
Author-email: {author_email}
"""

def exclude_pycache(tarinfo):
    if ('/__pycache__' in tarinfo.name) or tarinfo.name.endswith('.pyc'):
        return None
    return tarinfo

def auto_packages(pkgdir: str):
    """Discover subpackages and package_data"""
    pkgdir = os.path.normpath(pkgdir)
    pkg_name = os.path.basename(pkgdir)
    pkg_data = defaultdict(list)
    # Undocumented distutils feature: the empty string matches all package names
    pkg_data[''].append('*')
    packages = [pkg_name]
    subpkg_paths = set()

    def find_nearest_pkg(rel_path):
        parts = rel_path.split(os.sep)
        for i in reversed(range(1, len(parts))):
            ancestor = os.sep.join(parts[:i])
            if ancestor in subpkg_paths:
                pkg = '.'.join([pkg_name] + parts[:i])
                return pkg, os.sep.join(parts[i:])

        # Relative to the top-level package
        return pkg_name, rel_path

    for path, dirnames, filenames in os.walk(pkgdir, topdown=True):
        if os.path.basename(path) == '__pycache__':
            continue

        from_top_level = os.path.relpath(path, pkgdir)
        if from_top_level == '.':
            continue

        is_subpkg = '__init__.py' in filenames
        if is_subpkg:
            subpkg_paths.add(from_top_level)
            parts = from_top_level.split(os.sep)
            packages.append('.'.join([pkg_name] + parts))
        else:
            pkg, from_nearest_pkg = find_nearest_pkg(from_top_level)
            pkg_data[pkg].append(os.path.join(from_nearest_pkg, '*'))

    return packages, dict(pkg_data)

def _parse_req(requires_dist):
    """Parse "Foo (v); python_version == '2.x'" from Requires-Dist

    Returns pip-style appropriate for requirements.txt.
    """
    if ';' in requires_dist:
        name_version, env_mark = requires_dist.split(';', 1)
        env_mark = env_mark.strip()
    else:
        name_version, env_mark = requires_dist, None

    if '(' in name_version:
        # turn 'name (X)' and 'name (<X.Y)'
        # into 'name == X' and 'name < X.Y'
        name, version = name_version.split('(', 1)
        name = name.strip()
        version = version.replace(')', '').strip()
        if not any(c in version for c in '=<>'):
            version = '==' + version
        name_version = name + version

    return name_version, env_mark

def convert_requires(metadata):
    install_reqs = []
    extra_reqs = defaultdict(list)
    for req in metadata.requires_dist:
        name_version, env_mark = _parse_req(req)
        if env_mark is None:
            install_reqs.append(name_version)
        else:
            extra_reqs[':'+env_mark].append(name_version)

    return install_reqs, dict(extra_reqs)

def prep_entry_points(ini_info):
    # Write entry points
    cp = ConfigParser()

    if ini_info['scripts']:
        cp['console_scripts'] = {k: '%s:%s' % v
                                 for (k, v) in ini_info['scripts'].items()}

    if ini_info['entry_points_file'] is not None:
        cp.read(str(ini_info['entry_points_file']))
        if 'console_scripts' in cp:
            raise EntryPointsConflict

    res = defaultdict(list)
    for group in cp.sections():
        sect = cp[group]
        for name in sorted(sect):
            res[group].append('{} = {}'.format(name, sect[name]))

    return dict(res)

def make_sdist(ini_path=Path('flit.ini')):
    ini_info = inifile.read_pkg_ini(ini_path)
    module = common.Module(ini_info['module'], ini_path.parent)
    metadata = common.make_metadata(module, ini_info)

    target = ini_path.parent / 'dist' / '{}-{}.tar.gz'.format(metadata.name,
                                                              metadata.version)
    if not target.parent.exists():
        target.parent.mkdir(parents=True)
    tf = tarfile.open(str(target), mode='w:gz')
    tf_dir = '{}-{}'.format(metadata.name, metadata.version)

    srcdir = ini_path.parent

    vcs_mod = identify_vcs(srcdir)
    if vcs_mod.list_untracked_deleted_files(srcdir):
        raise VCSError("Untracked or deleted files in the source directory. "
                       "Commit, undo or ignore these files in your VCS.",
                       srcdir)

    for relpath in sorted(vcs_mod.list_tracked_files(srcdir)):
        path = srcdir / relpath
        tf.add(str(path), arcname=pjoin(tf_dir, relpath))

    before, extra = [], []
    if module.is_package:
        packages, package_data = auto_packages(str(module.path))
        before.append("packages = \\\n%s\n" % pformat(sorted(packages)))
        before.append("package_data = \\\n%s\n" % pformat(package_data))
        extra.append("packages=packages,".format([module.name]))
        extra.append("package_data=package_data,".format([module.name]))
    else:
        extra.append("py_modules={!r},".format([module.name]))

    install_reqs, extra_reqs = convert_requires(metadata)
    if install_reqs:
        before.append("install_requires = \\\n%s\n" % pformat(install_reqs))
        extra.append("install_requires=install_requires,")
    if extra_reqs:
        before.append("extras_require = \\\n%s\n" % pformat(extra_reqs))
        extra.append("extras_require=extras_require,")

    entrypoints = prep_entry_points(ini_info)
    if entrypoints:
        before.append("entry_points = \\\n%s\n" % pformat(entrypoints))
        extra.append("entry_points=entry_points,")

    if metadata.requires_python:
        extra.append('python_requires=%r,' % metadata.requires_python)

    setup_py = SETUP.format(
        before='\n'.join(before),
        name=metadata.name,
        version=metadata.version,
        description=metadata.summary,
        author=metadata.author,
        author_email=metadata.author_email,
        url=metadata.home_page,
        extra='\n      '.join(extra),
    ).encode('utf-8')
    ti = tarfile.TarInfo(pjoin(tf_dir, 'setup.py'))
    ti.size = len(setup_py)
    tf.addfile(ti, io.BytesIO(setup_py))

    pkg_info = PKG_INFO.format(
        name=metadata.name,
        version=metadata.version,
        summary=metadata.summary,
        home_page=metadata.home_page,
        author=metadata.author,
        author_email=metadata.author_email,
    ).encode('utf-8')
    ti = tarfile.TarInfo(pjoin(tf_dir, 'PKG-INFO'))
    ti.size = len(pkg_info)
    tf.addfile(ti, io.BytesIO(pkg_info))

    tf.close()

    print("Built", target)

if __name__ == '__main__':
    try:
        make_sdist()
    except VCSError as e:
        sys.exit(str(e))
