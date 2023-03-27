import datetime
import json
import os
from binascii import hexlify
from collections import defaultdict
from functools import partial
from getpass import getpass
from pathlib import Path

import click
import securesystemslib
import securesystemslib.exceptions
from taf.api.roles import _create_delegations, _initialize_roles_and_keystore, _role_obj
from taf.keys import (
    get_key_name,
    load_signing_keys,
    setup_roles_keys,
)
from taf.api.metadata import update_snapshot_and_timestamp
from taf.api.targets import (
    _get_namespace_and_root,
    _save_top_commit_of_repo_to_target,
    _update_target_repos,
    generate_repositories_json,
)
from taf.yubikey import export_yk_certificate
from tuf.repository_tool import (
    TARGETS_DIRECTORY_NAME,
    create_new_repository,
    generate_and_write_rsa_keypair,
)

from taf import YubikeyMissingLibrary
from taf.auth_repo import AuthenticationRepository
from taf.constants import (
    DEFAULT_ROLE_SETUP_PARAMS,
    DEFAULT_RSA_SIGNATURE_SCHEME,
    YUBIKEY_EXPIRATION_DATE,
)
from taf.exceptions import KeystoreError, TargetsMetadataUpdateError
from taf.git import GitRepository
from taf.repository_tool import (
    Repository,
    yubikey_signature_provider,
)
import taf.repositoriesdb as repositoriesdb

try:
    import taf.yubikey as yk
except ImportError:
    yk = YubikeyMissingLibrary()


def add_roles(
    repo_path,
    keystore=None,
    roles_key_infos=None,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
):
    yubikeys = defaultdict(dict)
    auth_repo = AuthenticationRepository(path=repo_path)
    repo_path = Path(repo_path)

    roles_key_infos, keystore = _initialize_roles_and_keystore(
        roles_key_infos, keystore
    )

    new_roles = []
    taf_repo = Repository(repo_path)
    existing_roles = taf_repo.get_all_targets_roles()
    main_roles = ["root", "snapshot", "timestamp", "targets"]
    existing_roles.extend(main_roles)

    # allow specification of roles without putting them inside targets delegations map
    # ensuring that it is possible to specify only delegated roles
    # since creation of delegations expects that structure, place the roles inside targets/delegations
    delegations_info = {}
    for role_name, role_data in dict(roles_key_infos["roles"]).items():
        if role_name not in main_roles:
            roles_key_infos["roles"].pop(role_name)
            delegations_info[role_name] = role_data
    roles_key_infos["roles"].setdefault("targets", {"delegations": {}})[
        "delegations"
    ].update(delegations_info)

    # find all existing roles which are parents of the newly added roles
    # they should be signed after the delegations are created
    roles = [
        (role_name, role_data)
        for role_name, role_data in roles_key_infos["roles"].items()
    ]
    parent_roles = set()
    while len(roles):
        role_name, role_data = roles.pop()
        for delegated_role, delegated_role_data in role_data.get(
            "delegations", {}
        ).items():
            if delegated_role not in existing_roles:
                if role_name not in new_roles:
                    parent_roles.add(role_name)
                new_roles.append(delegated_role)
            roles.append((delegated_role, delegated_role_data))

    if not len(new_roles):
        print("All roles already set up")
        return

    repository = taf_repo._repository
    roles_infos = roles_key_infos.get("roles")
    signing_keys, verification_keys = _load_sorted_keys_of_new_roles(
        auth_repo, roles_infos, taf_repo, keystore, yubikeys, existing_roles
    )
    _create_delegations(
        roles_infos, repository, verification_keys, signing_keys, existing_roles
    )
    for parent_role in parent_roles:
        _update_role(taf_repo, parent_role, keystore, roles_infos, scheme=scheme)
    update_snapshot_and_timestamp(taf_repo, keystore, roles_infos, scheme=scheme)


def update_target_repos_from_fs(
    repo_path, library_dir=None, namespace=None, add_branch=True
):
    """
    <Purpose>
        Create or update target files by reading the latest commits of the provided target repositories
    <Arguments>
        repo_path:
        Authentication repository's location
        library_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        add_branch:
        Indicates whether to add the current branch's name to the target file
    """
    repo_path = Path(repo_path).resolve()
    namespace, library_dir = _get_namespace_and_root(repo_path, namespace, library_dir)
    targets_directory = library_dir / namespace
    print(
        f"Updating target files corresponding to repos located at {targets_directory}"
    )
    auth_repo_targets_dir = repo_path / TARGETS_DIRECTORY_NAME
    if namespace:
        auth_repo_targets_dir = auth_repo_targets_dir / namespace
        auth_repo_targets_dir.mkdir(parents=True, exist_ok=True)
    for target_repo_path in targets_directory.glob("*"):
        _update_target_repos(
            repo_path, auth_repo_targets_dir, target_repo_path, add_branch
        )


def update_target_repos_from_repositories_json(
    repo_path, library_dir, namespace, add_branch=True
):
    """
    <Purpose>
        Create or update target files by reading the latest commit's repositories.json
    <Arguments>
        repo_path:
        Authentication repository's location
        library_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        add_branch:
        Indicates whether to add the current branch's name to the target file
    """
    repo_path = Path(repo_path).resolve()
    auth_repo_targets_dir = repo_path / TARGETS_DIRECTORY_NAME
    repositories_json = json.loads(
        Path(auth_repo_targets_dir / "repositories.json").read_text()
    )
    namespace, library_dir = _get_namespace_and_root(repo_path, namespace, library_dir)
    print(
        f"Updating target files corresponding to repos located at {(library_dir / namespace)}"
        "and specified in repositories.json"
    )
    for repo_name in repositories_json.get("repositories"):
        _save_top_commit_of_repo_to_target(
            library_dir, repo_name, repo_path, add_branch
        )


def _check_if_can_create_repository(auth_repo):
    repo_path = Path(auth_repo.path)
    if repo_path.is_dir():
        # check if there is non-empty metadata directory
        if auth_repo.metadata_path.is_dir() and any(auth_repo.metadata_path.iterdir()):
            if auth_repo.is_git_repository:
                print(
                    f'"{repo_path}" is a git repository containing the metadata directory. Generating new metadata files could make the repository invalid. Aborting.'
                )
                return False
            if not click.confirm(
                f'Metadata directory found inside "{repo_path}". Recreate metadata files?'
            ):
                return False
    return True


def create_repository(
    repo_path, keystore=None, roles_key_infos=None, commit=False, test=False
):
    """
    <Purpose>
        Create a new authentication repository. Generate initial metadata files.
        The initial targets metadata file is empty (does not specify any targets).
    <Arguments>
        repo_path:
        Authentication repository's location
        targets_directory:
        Directory which contains target repositories
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        commit:
        Indicates if the changes should be automatically committed
        test:
        Indicates if the created repository is a test authentication repository
    """
    yubikeys = defaultdict(dict)
    auth_repo = AuthenticationRepository(path=repo_path)
    repo_path = Path(repo_path)

    if not _check_if_can_create_repository(auth_repo):
        return

    roles_key_infos, keystore = _initialize_roles_and_keystore(
        roles_key_infos, keystore
    )

    repository = create_new_repository(str(auth_repo.path))
    roles_infos = roles_key_infos.get("roles")
    signing_keys, verification_keys = _load_sorted_keys_of_new_roles(
        auth_repo, roles_infos, repository, keystore, yubikeys
    )
    # set threshold and register keys of main roles
    # we cannot do the same for the delegated roles until delegations are created
    for role_name, role_key_info in roles_infos.items():
        threshold = role_key_info.get("threshold", 1)
        is_yubikey = role_key_info.get("yubikey", False)
        _setup_role(
            role_name,
            threshold,
            is_yubikey,
            repository,
            verification_keys[role_name],
            signing_keys.get(role_name),
        )

    _create_delegations(roles_infos, repository, verification_keys, signing_keys)

    # if the repository is a test repository, add a target file called test-auth-repo
    if test:
        test_auth_file = (
            Path(auth_repo.path, auth_repo.targets_path) / auth_repo.TEST_REPO_FLAG_FILE
        )
        test_auth_file.touch()

    # register and sign target files (if any)
    try:
        taf_repository = Repository(repo_path)
        taf_repository._tuf_repository = repository
        register_target_files(
            repo_path, keystore, roles_key_infos, commit=commit, taf_repo=taf_repository
        )
    except TargetsMetadataUpdateError:
        # if there are no target files
        repository.writeall()

    print("Created new authentication repository")

    if commit:
        auth_repo.init_repo()
        commit_message = input("\nEnter commit message and press ENTER\n\n")
        auth_repo.commit(commit_message)



def _load_sorted_keys_of_new_roles(
    auth_repo, roles_infos, repository, keystore, yubikeys, existing_roles=None
):
    def _sort_roles(key_info, repository):
        # load keys not stored on YubiKeys first, to avoid entering pins
        # if there is somethig wrong with keystore files
        keystore_roles = []
        yubikey_roles = []
        for role_name, role_key_info in key_info.items():
            if not role_key_info.get("yubikey", False):
                keystore_roles.append((role_name, role_key_info))
            else:
                yubikey_roles.append((role_name, role_key_info))
            if "delegations" in role_key_info:
                delegated_keystore_role, delegated_yubikey_roles = _sort_roles(
                    role_key_info["delegations"]["roles"], repository
                )
                keystore_roles.extend(delegated_keystore_role)
                yubikey_roles.extend(delegated_yubikey_roles)
        return keystore_roles, yubikey_roles

    # load and/or generate all keys first
    if existing_roles is None:
        existing_roles = []
    try:
        keystore_roles, yubikey_roles = _sort_roles(roles_infos, repository)
        signing_keys = {}
        verification_keys = {}
        for role_name, key_info in keystore_roles:
            if role_name in existing_roles:
                continue
            keystore_keys, _ = setup_roles_keys(
                role_name, key_info, repository, keystore=keystore
            )
            for public_key, private_key in keystore_keys:
                signing_keys.setdefault(role_name, []).append(private_key)
                verification_keys.setdefault(role_name, []).append(public_key)

        for role_name, key_info in yubikey_roles:
            if role_name in existing_roles:
                continue
            _, yubikey_keys = setup_roles_keys(
                role_name,
                key_info,
                repository,
                certs_dir=auth_repo.certs_dir,
                yubikeys=yubikeys,
            )
            verification_keys[role_name] = yubikey_keys
        return signing_keys, verification_keys
    except KeystoreError as e:
        print(f"Creation of repository failed: {e}")
        return


def export_yk_public_pem(path=None):
    try:
        pub_key_pem = yk.export_piv_pub_key().decode("utf-8")
    except Exception:
        print("Could not export the public key. Check if a YubiKey is inserted")
        return
    if path is None:
        print(pub_key_pem)
    else:
        if not path.endswith(".pub"):
            path = f"{path}.pub"
        path = Path(path)
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pub_key_pem)


def generate_keys(keystore, roles_key_infos):
    """
    <Purpose>
        Generate public and private keys and writes them to disk. Names of keys correspond to names
        of the TUF roles. If more than one key should be generated per role, a counter is appended
        to the role's name. E.g. root1, root2, root3 etc.
    <Arguments>
        keystore:
        Location where the generated files should be saved
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        This includes:
            - passwords of the keystore files
            - number of keys per role (optional, defaults to one if not provided)
            - key length (optional, defaults to TUF's default value, which is 3072)
        Names of the keys are set to names of the roles plus a counter, if more than one key
        should be generated.
    """
    roles_key_infos, keystore = _initialize_roles_and_keystore(
        roles_key_infos, keystore
    )

    for role_name, key_info in roles_key_infos["roles"].items():
        num_of_keys = key_info.get("number", DEFAULT_ROLE_SETUP_PARAMS["number"])
        bits = key_info.get("length", DEFAULT_ROLE_SETUP_PARAMS["length"])
        passwords = key_info.get("passwords", [""] * num_of_keys)
        is_yubikey = key_info.get("yubikey", DEFAULT_ROLE_SETUP_PARAMS["yubikey"])
        for key_num in range(num_of_keys):
            if not is_yubikey:
                key_name = get_key_name(role_name, key_num, num_of_keys)
                password = passwords[key_num]
                path = str(Path(keystore, key_name))
                print(f"Generating {path}")
                generate_and_write_rsa_keypair(
                    filepath=path, bits=bits, password=password
                )
        if "delegations" in key_info:
            generate_keys(keystore, key_info["delegations"])



def register_target_files(
    repo_path,
    keystore=None,
    roles_key_infos=None,
    commit=False,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
    taf_repo=None,
):
    """
    <Purpose>
        Register all files found in the target directory as targets - updates the targets
        metadata file, snapshot and timestamp. Sign targets
        with yubikey if keystore is not provided
    <Arguments>
        repo_path:
        Authentication repository's path
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        commit_msg:
        Commit message. If specified, the changes made to the authentication are committed.
        scheme:
        A signature scheme used for signing.
        taf_repo:
        If taf repository is already initialized, it can be passed and used.
    """
    print("Signing target files")
    roles_key_infos, keystore = _initialize_roles_and_keystore(
        roles_key_infos, keystore, enter_info=False
    )
    roles_infos = roles_key_infos.get("roles")
    if taf_repo is None:
        repo_path = Path(repo_path).resolve()
        taf_repo = Repository(str(repo_path))

    # find files that should be added/modified/removed
    added_targets_data, removed_targets_data = taf_repo.get_all_target_files_state()

    _update_target_roles(
        taf_repo,
        added_targets_data,
        removed_targets_data,
        keystore,
        roles_infos,
        scheme,
    )

    if commit:
        auth_git_repo = GitRepository(path=taf_repo.path)
        commit_message = input("\nEnter commit message and press ENTER\n\n")
        auth_git_repo.commit(commit_message)


def signature_provider(key_id, cert_cn, key, data):  # pylint: disable=W0613
    def _check_key_id(expected_key_id):
        try:
            inserted_key = yk.get_piv_public_key_tuf()
            return expected_key_id == inserted_key["keyid"]
        except Exception:
            return False

    while not _check_key_id(key_id):
        pass

    data = securesystemslib.formats.encode_canonical(data).encode("utf-8")
    key_pin = getpass(f"Please insert {cert_cn} YubiKey, input PIN and press ENTER.\n")
    signature = yk.sign_piv_rsa_pkcs1v15(data, key_pin)

    return {"keyid": key_id, "sig": hexlify(signature).decode()}


def _setup_role(
    role_name,
    threshold,
    is_yubikey,
    repository,
    verification_keys,
    signing_keys=None,
    parent=None,
):
    role_obj = _role_obj(role_name, repository, parent)
    role_obj.threshold = threshold
    if not is_yubikey:
        for public_key, private_key in zip(verification_keys, signing_keys):
            role_obj.add_verification_key(public_key)
            role_obj.load_signing_key(private_key)
    else:
        for key_num, key in enumerate(verification_keys):
            key_name = get_key_name(role_name, key_num, len(verification_keys))
            role_obj.add_verification_key(key, expires=YUBIKEY_EXPIRATION_DATE)
            role_obj.add_external_signature_provider(
                key, partial(yubikey_signature_provider, key_name, key["keyid"])
            )


def setup_signing_yubikey(certs_dir=None, scheme=DEFAULT_RSA_SIGNATURE_SCHEME):
    if not click.confirm(
        "WARNING - this will delete everything from the inserted key. Proceed?"
    ):
        return
    _, serial_num = yk.yubikey_prompt(
        "new Yubikey",
        creating_new_key=True,
        pin_confirm=True,
        pin_repeat=True,
        prompt_message="Please insert the new Yubikey and press ENTER",
    )
    key = yk.setup_new_yubikey(serial_num)
    export_yk_certificate(certs_dir, key)


def setup_test_yubikey(key_path=None):
    """
    Resets the inserted yubikey, sets default pin and copies the specified key
    onto it.
    """
    if not click.confirm("WARNING - this will reset the inserted key. Proceed?"):
        return
    key_path = Path(key_path)
    key_pem = key_path.read_bytes()

    print(f"Importing RSA private key from {key_path} to Yubikey...")
    pin = yk.DEFAULT_PIN

    pub_key = yk.setup(pin, "Test Yubikey", private_key_pem=key_pem)
    print("\nPrivate key successfully imported.\n")
    print("\nPublic key (PEM): \n{}".format(pub_key.decode("utf-8")))
    print("Pin: {}\n".format(pin))


def update_and_sign_targets(
    repo_path: str,
    library_dir: str,
    target_types: list,
    keystore: str,
    roles_key_infos: str,
    scheme: str,
):
    """
    <Purpose>
        Save the top commit of specified target repositories to the corresponding target files and sign
    <Arguments>
        repo_path:
        Authentication repository's location
        library_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        targets:
        Types of target repositories whose corresponding target files should be updated and signed
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys
        no_commit:
        Indicates that the changes should bot get committed automatically
        scheme:
        A signature scheme used for signing

    """
    auth_path = Path(repo_path).resolve()
    auth_repo = AuthenticationRepository(path=auth_path)
    if library_dir is None:
        library_dir = auth_path.parent.parent
    repositoriesdb.load_repositories(auth_repo)
    nonexistent_target_types = []
    target_names = []
    for target_type in target_types:
        try:
            target_name = repositoriesdb.get_repositories_paths_by_custom_data(
                auth_repo, type=target_type
            )[0]
            target_names.append(target_name)
        except Exception:
            nonexistent_target_types.append(target_type)
            continue
    if len(nonexistent_target_types):
        print(
            f"Target types {'.'.join(nonexistent_target_types)} not in repositories.json. Targets not updated"
        )
        return

    # only update target files if all specified types are valid
    for target_name in target_names:
        _save_top_commit_of_repo_to_target(library_dir, target_name, auth_path, True)
        print(f"Updated {target_name} target file")
    register_target_files(auth_path, keystore, roles_key_infos, True, scheme)


def _update_role(taf_repo, role, keystore, roles_infos, scheme):
    keystore_keys, yubikeys = load_signing_keys(
        taf_repo, role, keystore, roles_infos, scheme=scheme
    )
    if len(keystore_keys):
        taf_repo.update_role_keystores(role, keystore_keys, write=False)
    if len(yubikeys):
        taf_repo.update_role_yubikeys(role, yubikeys, write=False)


def _update_target_roles(
    taf_repo,
    added_targets_data,
    removed_targets_data,
    keystore,
    roles_infos,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
):
    """Update given targets data with an appropriate role, as well as snapshot and
    timestamp roles.
    """
    added_targets_data = {} if added_targets_data is None else added_targets_data
    removed_targets_data = {} if removed_targets_data is None else removed_targets_data

    roles_targets = taf_repo.roles_targets_for_filenames(
        list(added_targets_data.keys()) + list(removed_targets_data.keys())
    )

    if not roles_targets:
        raise TargetsMetadataUpdateError(
            "There are no added/modified/removed target files."
        )

    # update targets
    loaded_yubikeys = {}
    for role, target_paths in roles_targets.items():
        keystore_keys, yubikeys = load_signing_keys(
            taf_repo, role, keystore, roles_infos, loaded_yubikeys, scheme=scheme
        )
        targets_data = dict(
            added_targets_data={
                path: val
                for path, val in added_targets_data.items()
                if path in target_paths
            },
            removed_targets_data={
                path: val
                for path, val in removed_targets_data.items()
                if path in target_paths
            },
        )

        if len(yubikeys):
            taf_repo.update_targets_yubikeys(yubikeys, write=False, **targets_data)
        if len(keystore_keys):
            taf_repo.update_targets_keystores(
                keystore_keys, write=False, **targets_data
            )

    # update other roles and writeall
    update_snapshot_and_timestamp(taf_repo, keystore, roles_infos, scheme=scheme)


# TODO Implement update of repositories.json (updating urls, custom data, adding new repository, removing
# repository etc.)
# TODO create tests for this
