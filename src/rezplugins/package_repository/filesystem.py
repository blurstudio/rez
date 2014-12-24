"""
Filesystem-based package repository
"""
from rez.package_repository import PackageRepository
from rez.package_resources_ import PackageFamilyResource, PackageResource, \
    DerivedVariantResource, PackageResourceHelper, package_pod_schema
from rez.exceptions import PackageMetadataError
from rez.utils.formatting import is_valid_package_name, PackageRequest
from rez.utils.resources import cached_property
from rez.serialise import load_from_file, FileFormat
from rez.config import config
from rez.memcache import mem_cached, DataType
from rez.backport.lru_cache import lru_cache
from rez.vendor.schema.schema import Schema, Optional, And, Use
from rez.vendor.version.version import Version, VersionRange
import os.path
import os


#------------------------------------------------------------------------------
# utility functions
#------------------------------------------------------------------------------

# get a file that could be .yaml or .py
def _get_file(path, name):
    for format_ in (FileFormat.py, FileFormat.yaml):
        filename = "%s.%s" % (name, format_.extension)
        filepath = os.path.join(path, filename)
        if os.path.isfile(filepath):
            return filepath, format_
    return None, None


#------------------------------------------------------------------------------
# resources
#------------------------------------------------------------------------------

class FileSystemPackageFamilyResource(PackageFamilyResource):
    key = "filesystem.family"
    repository_type = "filesystem"

    def _uri(self):
        return self.path

    @cached_property
    def path(self):
        return os.path.join(self.location, self.name)

    def get_last_release_time(self):
        # this repository makes sure to update path mtime every time a
        # variant is added to the repository [TODO: coming]
        path = os.path.join(self.location, self.name)
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0

    def iter_packages(self):
        # check for unversioned package
        filepath, _ = _get_file(self.path, "package")
        if filepath:
            package = self._repository.get_resource(
                FileSystemPackageResource.key,
                location=self.location,
                name=self.name)
            yield package
            return

        # versioned packages
        for version_str in self._repository._get_version_dirs(self.path):
            package = self._repository.get_resource(
                FileSystemPackageResource.key,
                location=self.location,
                name=self.name,
                version=version_str)
            yield package


class FileSystemPackageResource(PackageResourceHelper):
    key = "filesystem.package"
    variant_key = "filesystem.variant"
    repository_type = "filesystem"
    schema = package_pod_schema

    def _uri(self):
        return self.filepath

    @cached_property
    def parent(self):
        family = self._repository.get_resource(
            FileSystemPackageFamilyResource.key,
            location=self.location,
            name=self.name)
        return family

    @cached_property
    def state_handle(self):
        if self.filepath:
            return os.path.getmtime(self.filepath)
        return None

    @property
    def base(self):
        return self.path

    @cached_property
    def path(self):
        path = os.path.join(self.location, self.name)
        ver_str = self.get("version")
        if ver_str:
            path = os.path.join(path, ver_str)
        return path

    @cached_property
    def filepath(self):
        return self._filepath_and_format[0]

    @cached_property
    def file_format(self):
        return self._filepath_and_format[1]

    @cached_property
    def _filepath_and_format(self):
        return _get_file(self.path, "package")

    def _load(self):
        if self.filepath is None:
            raise PackageMetadataError("Missing package definition file: %r" % self)

        data = load_from_file(self.filepath, self.file_format)

        if "timestamp" not in data:  # old format support
            data_ = self._load_old_formats()
            if data_:
                data.update(data_)

        return data

    def _load_old_formats(self):
        data = None
        path = self.uri

        filepath = os.path.join(path, "release.yaml")
        if os.path.isfile(filepath):
            # rez<2.0.BETA.16
            data = load_from_file(filepath, FileFormat.yaml,
                                  update_data_callback=self._update_changelog)
        else:
            path_ = os.path.join(path, ".metadata")
            if os.path.isdir(path_):
                # rez-1
                data = {}
                filepath = os.path.join(path_, "changelog.txt")
                if os.path.isfile(filepath):
                    data["changelog"] = load_from_file(
                        filepath, FileFormat.txt,
                        update_data_callback=self._update_changelog)

                filepath = os.path.join(path_, "release_time.txt")
                if os.path.isfile(filepath):
                    value = load_from_file(filepath, FileFormat.txt)
                    try:
                        data["timestamp"] = int(value.strip())
                    except:
                        pass
        return data

    def _update_changelog(self, file_format, data):
        # this is to deal with older package releases. They can contain long
        # changelogs (more recent rez versions truncate before release), and
        # release.yaml files can contain a list-of-str changelog.
        maxlen = config.max_package_changelog_chars

        if file_format == FileFormat.yaml:
            changelog = data.get("changelog")
            if changelog:
                changed = False
                if isinstance(changelog, list):
                    changelog = '\n'.join(changelog)
                    changed = True
                if len(changelog) > (maxlen + 3):
                    changelog = changelog[:maxlen] + "..."
                    changed = True
                if changed:
                    data["changelog"] = changelog
        else:
            assert isinstance(data, basestring)
            if len(data) > (maxlen + 3):
                data = data[:maxlen] + "..."

        return data


class FileSystemVariantResource(DerivedVariantResource):
    key = "filesystem.variant"
    repository_type = "filesystem"

    @cached_property
    def parent(self):
        package = self._repository.get_resource(
            FileSystemPackageResource.key,
            location=self.location,
            name=self.name,
            version=self.get("version"))
        return package


# -- 'combined' resource types

class FileSystemCombinedPackageFamilyResource(PackageFamilyResource):
    key = "filesystem.family.combined"
    repository_type = "filesystem"

    schema = Schema({
        Optional("versions"):               [And(basestring,
                                                 Use(Version))],
        Optional("version_overrides"):      {And(basestring,
                                                 Use(VersionRange)): dict}
    })

    @property
    def ext(self):
        return self.get("ext")

    @property
    def filepath(self):
        filename = "%s.%s" % (self.name, self.ext)
        return os.path.join(self.location, filename)

    def _uri(self):
        return self.filepath

    def get_last_release_time(self):
        try:
            return os.path.getmtime(self.filepath)
        except OSError:
            return 0

    def iter_packages(self):
        # unversioned package
        if not self.versions:
            package = self._repository.get_resource(
                FileSystemCombinedPackageResource.key,
                location=self.location,
                name=self.name,
                ext=self.ext)
            yield package
            return

        # versioned packages
        for version in self.versions:
            package = self._repository.get_resource(
                FileSystemCombinedPackageResource.key,
                location=self.location,
                name=self.name,
                ext=self.ext,
                version=str(version))
            yield package

    def _load(self):
        format_ = FileFormat[self.ext]
        data = load_from_file(self.filepath, format_)
        return data


class FileSystemCombinedPackageResource(PackageResourceHelper):
    key = "filesystem.package.combined"
    variant_key = "filesystem.variant.combined"
    repository_type = "filesystem"
    schema = package_pod_schema

    def _uri(self):
        ver_str = self.get("version", "")
        return "%s<%s>" % (self.parent.filepath, ver_str)

    @cached_property
    def parent(self):
        family = self._repository.get_resource(
            FileSystemCombinedPackageFamilyResource.key,
            location=self.location,
            name=self.name,
            ext=self.get("ext"))
        return family

    @property
    def base(self):
        return None  # combined resource types do not have 'base'

    @cached_property
    def state_handle(self):
        return os.path.getmtime(self.parent.filepath)

    def iter_variants(self):
        num_variants = len(self.data.get("variants", []))
        if num_variants == 0:
            indexes = [None]
        else:
            indexes = range(num_variants)

        for index in indexes:
            variant = self._repository.get_resource(
                self.variant_key,
                location=self.location,
                name=self.name,
                ext=self.get("ext"),
                version=self.get("version"),
                index=index)
            yield variant

    def _load(self):
        data = self.parent.data.copy()

        if "versions" in data:
            del data["versions"]
            version_str = self.get("version")
            data["version"] = version_str
            version = Version(version_str)

            overrides = self.parent.version_overrides
            if overrides:
                for range_, data_ in overrides.iteritems():
                    if version in range_:
                        data.update(data_)
                del data["version_overrides"]

        return data


class FileSystemCombinedVariantResource(DerivedVariantResource):
    key = "filesystem.variant.combined"
    repository_type = "filesystem"

    @cached_property
    def parent(self):
        package = self._repository.get_resource(
            FileSystemCombinedPackageResource.key,
            location=self.location,
            name=self.name,
            ext=self.get("ext"),
            version=self.get("version"))
        return package

    @property
    def root(self):
        return None  # combined resource types do not have 'root'


#------------------------------------------------------------------------------
# repository
#------------------------------------------------------------------------------

class FileSystemPackageRepository(PackageRepository):
    """A filesystem-based package repository.

    Packages are stored on disk, in either 'package.yaml' or 'package.py' files.
    These files are stored into an organised directory structure like so:

        /LOCATION/pkgA/1.0.0/package.py
                      /1.0.1/package.py
                 /pkgB/2.1/package.py
                      /2.2/package.py

    Another supported storage format is to store all package versions within a
    single package family in one file, like so:

        /LOCATION/pkgC.yaml
        /LOCATION/pkgD.py

    These 'combined' package files allow for differences between package
    versions via a 'package_overrides' section:

        name: pkgC

        versions:
        - '1.0'
        - '1.1'
        - '1.2'

        version_overrides:
            '1.0':
                requires:
                - python-2.5
            '1.1+':
                requires:
                - python-2.6
    """
    @classmethod
    def name(cls):
        return "filesystem"

    def __init__(self, location, resource_pool):
        """Create a filesystem package repository.

        Args:
            location (str): Path containing the package repository.
        """
        super(FileSystemPackageRepository, self).__init__(location, resource_pool)
        self.register_resource(FileSystemPackageFamilyResource)
        self.register_resource(FileSystemPackageResource)
        self.register_resource(FileSystemVariantResource)

        self.register_resource(FileSystemCombinedPackageFamilyResource)
        self.register_resource(FileSystemCombinedPackageResource)
        self.register_resource(FileSystemCombinedVariantResource)

    def _uid(self):
        st = os.stat(self.location)
        return ("filesystem", self.location, st.st_ino)

    def get_package_family(self, name):
        return self._get_family(name)

    def iter_package_families(self):
        for family in self._get_families():
            yield family

    def iter_packages(self, package_family_resource):
        for package in self._get_packages(package_family_resource):
            yield package

    def iter_variants(self, package_resource):
        for variant in self._get_variants(package_resource):
            yield variant

    def get_parent_package_family(self, package_resource):
        return package_resource.parent

    def get_parent_package(self, variant_resource):
        return variant_resource.parent

    def get_variant_state_handle(self, variant_resource):
        package_resource = variant_resource.parent
        return package_resource.state_handle

    def get_last_release_time(self, package_family_resource):
        return package_family_resource.get_last_release_time()

    # -- internal

    def _get_family_dirs__key(self):
        st = os.stat(self.location)
        return (self.location, st.st_ino, st.st_mtime)

    @mem_cached(DataType.listdir, key_func=_get_family_dirs__key)
    def _get_family_dirs(self):
        dirs = []
        for name in os.listdir(self.location):
            path = os.path.join(self.location, name)
            if os.path.isdir(path):
                if is_valid_package_name(name):
                    dirs.append((name, None))
            else:
                name_, ext_ = os.path.splitext(name)
                if ext_ in (".py", ".yaml") and is_valid_package_name(name_):
                    dirs.append((name_, ext_[1:]))
        return dirs

    def _get_version_dirs__key(self, root):
        st = os.stat(root)
        return (root, st.st_ino, st.st_mtime)

    @mem_cached(DataType.listdir, key_func=_get_version_dirs__key)
    def _get_version_dirs(self, root):
        dirs = []
        for name in os.listdir(root):
            if name.startswith('.'):
                continue
            path = os.path.join(root, name)
            if os.path.isdir(path):
                dirs.append(name)
        return dirs

    @lru_cache(maxsize=None)
    def _get_families(self):
        families = []
        for name, ext in self._get_family_dirs():
            if ext is None:  # is a directory
                family = self.get_resource(
                    FileSystemPackageFamilyResource.key,
                    location=self.location,
                    name=name)
            else:
                family = self.get_resource(
                    FileSystemCombinedPackageFamilyResource.key,
                    location=self.location,
                    name=name,
                    ext=ext)
            families.append(family)
        return families

    @lru_cache(maxsize=None)
    def _get_family(self, name):
        is_valid_package_name(name, raise_error=True)
        if os.path.isdir(os.path.join(self.location, name)):
            family = self.get_resource(
                FileSystemPackageFamilyResource.key,
                location=self.location,
                name=name)
            return family
        else:
            filepath, format_ = _get_file(self.location, name)
            if filepath:
                family = self.get_resource(
                    FileSystemCombinedPackageFamilyResource.key,
                    location=self.location,
                    name=name,
                    ext=format_.extension)
                return family
        return None

    @lru_cache(maxsize=None)
    def _get_packages(self, package_family_resource):
        return [x for x in package_family_resource.iter_packages()]

    @lru_cache(maxsize=None)
    def _get_variants(self, package_resource):
        return [x for x in package_resource.iter_variants()]


def register_plugin():
    return FileSystemPackageRepository