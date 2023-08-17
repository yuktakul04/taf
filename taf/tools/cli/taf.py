import click
import taf.tools.dependencies as dependencies_cli
import taf.tools.keystore as keystore_cli
import taf.tools.repo as repo_cli
import taf.tools.targets as targets_cli
import taf.tools.metadata as metadata_cli
import taf.tools.yubikey as yubikey_cli
import taf.tools.roles as roles_cli


@click.group()
@click.version_option()
def taf():
    """TAF Command Line Interface"""
    pass


dependencies_cli.attach_to_group(taf)
keystore_cli.attach_to_group(taf)
repo_cli.attach_to_group(taf)
targets_cli.attach_to_group(taf)
metadata_cli.attach_to_group(taf)
yubikey_cli.attach_to_group(taf)
roles_cli.attach_to_group(taf)


taf()
