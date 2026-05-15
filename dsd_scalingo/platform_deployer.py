"""Manages all Scalingo-specific aspects of the deployment process.

Notes:
- 

Add a new file to the user's project, using a template:

    def _add_dockerfile(self):
        # Add a minimal dockerfile.
        template_path = self.templates_path / "dockerfile_example"
        context = {
            "django_project_name": dsd_config.local_project_name,
        }
        contents = plugin_utils.get_template_string(template_path, context)

        # Write file to project.
        path = dsd_config.project_root / "Dockerfile"
        plugin_utils.add_file(path, contents)

Modify user's settings file:

    def _modify_settings(self):
        # Add platformsh-specific settings.
        template_path = self.templates_path / "settings.py"
        context = {
            "deployed_project_name": self._get_deployed_project_name(),
        }
        plugin_utils.modify_settings_file(template_path, context)
"""

import sys, os, re, json
from pathlib import Path
import time
import secrets

from django.utils.safestring import mark_safe

import requests

from . import deploy_messages as platform_msgs
from .plugin_config import plugin_config
from . import utils as scalingo_utils
from . import key_utils

from django_simple_deploy.management.commands.utils import plugin_utils
from django_simple_deploy.management.commands.utils.plugin_utils import dsd_config
from django_simple_deploy.management.commands.utils.command_errors import DSDCommandError


class PlatformDeployer:
    """Perform the initial deployment to Scalingo

    If --automate-all is used, carry out an actual deployment.
    If not, do all configuration work so the user only has to commit changes, and ...
    """

    def __init__(self):
        self.templates_path = Path(__file__).parent / "templates"

    # --- Public methods ---

    def deploy(self, *args, **options):
        """Coordinate the overall configuration and deployment."""
        plugin_utils.write_output("\nConfiguring project for deployment to Scalingo...")

        self._validate_platform()
        self._prep_automate_all()
        self._prep_config_only()

        # Configure project for deployment to Scalingo
        self._add_python_version()
        self._add_procfile()
        self._add_bin_post_deploy()
        self._add_requirements()
        self._modify_settings()

        self._conclude_automate_all()
        self._show_success_message()

    # --- Helper methods for deploy() ---

    def _validate_platform(self):
        """Make sure the local environment and project supports deployment to Scalingo.

        Returns:
            None
        Raises:
            DSDCommandError: If we find any reason deployment won't work.
        """
        ...
        # DEV: Consider `scalingo self` or `scalingo whoami`
        if dsd_config.unit_testing:
            self.app_name = dsd_config.deployed_project_name
            return

        plugin_utils.write_output("Validating Scalingo CLI...")
        
        # Make sure CLI is installed.
        cmd = "scalingo --version"
        try:
            output_obj = plugin_utils.run_quick_command(cmd)
        except FileNotFoundError:
            raise DSDCommandError(platform_msgs.cli_not_installed)
        
        if "scalingo version " not in output_obj.stdout.decode():
            raise DSDCommandError(platform_msgs.cli_not_installed)
        
        cmd = "scalingo whoami"
        output_obj = plugin_utils.run_quick_command(cmd)

        if "You are logged in as " not in output_obj.stdout.decode():
            raise DSDCommandError(platform_msgs.cli_logged_out)

        # Check that at least one SSH key has been uploaded.
        cmd = "scalingo keys"
        output_obj = plugin_utils.run_quick_command(cmd)
        output_str = output_obj.stdout.decode().strip()
        if output_str == "┌──────┬─────────┐\n│ NAME │ CONTENT │\n└──────┴─────────┘":
            if plugin_config.key_assist:
                key_uploaded = key_utils.key_assist()
                if not key_uploaded:
                    raise DSDCommandError(platform_msgs.no_ssh_keys)
            else:
                raise DSDCommandError(platform_msgs.no_ssh_keys)

        plugin_utils.write_output("  CLI is installed and authenticated.")

        if dsd_config.automate_all:
            # No more validation to do for fully-automated workflows.
            return

        # Validate that required resources have been created for a
        # configuration-only workflow.
        new_apps = scalingo_utils.get_new_apps()

        if len(new_apps) == 0:
            raise DSDCommandError(platform_msgs.no_remote_project)

        if len(new_apps) == 1:
            app_name = new_apps[0]
            msg = platform_msgs.use_scalingo_app(app_name)
            confirmed = plugin_utils.get_confirmation(msg)

            if not confirmed:
                raise DSDCommandError(platform_msgs.no_remote_project)
            else:
                msg = f"\nOkay, deploying to {app_name}..."
                dsd_config.deployed_project_name = app_name
                self.app_name = app_name
                plugin_utils.write_output(msg)
                return

        # There's more than one Scalingo app with a new status.
        # We're not handling this case now.
        raise DSDCommandError(platform_msgs.multiple_new_apps)


    def _prep_automate_all(self):
        """Take any further actions needed if using automate_all."""
        if not dsd_config.automate_all:
            return

        # DEV: Consider creating these resources as late as possible.
        # Create a new project on Scalingo.
        msg = "  Creating a new app on Scalingo..."
        plugin_utils.write_output(msg)

        # Set the Scalingo project name.
        if not dsd_config.deployed_project_name:
            # Scalingo requires every project name on the platform to be unique.
            # Add a random 8-character token to the name. This only applies if we're
            # generating the name. When `--deployed-project-name` is used, the
            # user probably wants to set the name themselves.
            # This also means we don't need to check the length of the name,
            # because it will always pass the minimum length. (6-48 chars)
            dsd_config.deployed_project_name = dsd_config.local_project_name.replace("_", "-")
            dsd_config.deployed_project_name += f"-{secrets.token_hex(4)}"
        
        self.app_name = dsd_config.deployed_project_name

        # Issue CLI command to generate new Scalingo project.
        cmd = f"scalingo create {dsd_config.deployed_project_name}"
        output_obj = plugin_utils.run_quick_command(cmd)
        output_str = output_obj.stdout.decode()
        plugin_utils.write_output(output_str)

        # If the remote project already exists, bail.
        if "name → has already been taken" in output_str:
            raise DSDCommandError(platform_msgs.project_already_exists)

        self._create_postgres_db()

    def _prep_config_only(self):
        """Complete any work needed to support the configuration-only workflow."""
        if dsd_config.automate_all or dsd_config.unit_testing:
            return
            
        # Create a db, assuming the remote app does not already have one.
        existing_dbs = scalingo_utils.get_existing_dbs(self.app_name)
        if not existing_dbs:
            self._create_postgres_db()
        else:
            msg = platform_msgs.found_existing_db(self.app_name, existing_dbs)
            raise DSDCommandError(msg)

    def _create_postgres_db(self):
        """Create a remote Postgres db."""
        msg = "  Creating a new Postgres db..."
        plugin_utils.write_output(msg)

        cmd = f"scalingo --app {self.app_name} addons-add postgresql postgresql-starter-512"
        output_obj = plugin_utils.run_quick_command(cmd)
        output_str = output_obj.stdout.decode()
        plugin_utils.write_output(output_str)

        # DEV: Write a loop to poll for a running db instance. Query for addons, look at status
        # of PostgreSQL instance.
        time.sleep(30)

    def _add_python_version(self):
        """Add a .python-version file."""
        path = dsd_config.project_root / ".python-version"
        plugin_utils.add_file(path, contents="3.14")

    def _add_procfile(self):
        """Add a Procfile."""
        path = dsd_config.project_root / "Procfile"
        contents = f"web: gunicorn {dsd_config.local_project_name}.wsgi --log-file -"
        contents += "\npostdeploy: bash bin/post_deploy.sh"
        plugin_utils.add_file(path, contents)

    def _add_bin_post_deploy(self):
        """Add a bin/post_deploy.sh file."""
        path_bin = dsd_config.project_root / "bin"
        plugin_utils.add_dir(path_bin)

        path_post_deploy = path_bin / "post_deploy.sh"
        contents = "#!/bin/sh\n\npython manage.py migrate\n"
        # Make sure that the file has Unix-style (line feed) line terminators.
        # Under Windows the default is carriage return + line feed which
        # would cause "command not found" errors when post_deploy.sh runs
        # in a Scalingo Ubuntu container.
        plugin_utils.add_file(path_post_deploy, contents, newline='\n')

    def _add_requirements(self):
        """Add requirements for deploying to Scalingo."""
        requirements = ["gunicorn", "psycopg2", "dj-database-url", "whitenoise", "dj-static"]
        plugin_utils.add_packages(requirements)

    def _modify_settings(self):
        # Add Scalingo-specific settings.
        template_path = self.templates_path / "settings.py"
        context = {
            "deployed_project_name": self.app_name,
        }
        plugin_utils.modify_settings_file(template_path, context)

    def _conclude_automate_all(self):
        """Finish automating the push to Scalingo.

        - Commit all changes.
        - ...
        """
        # Making this check here lets deploy() be cleaner.
        if not dsd_config.automate_all:
            return

        plugin_utils.commit_changes()

        # Push project.
        plugin_utils.write_output("  Deploying to Scalingo...")
        cmd = "git push scalingo main"
        plugin_utils.run_slow_command(cmd)

        # Open project.
        plugin_utils.write_output("  Opening deployed app in a new browser tab...")
        cmd = f"scalingo --app {self.app_name} open"
        output = plugin_utils.run_quick_command(cmd)
        plugin_utils.write_output(output)

        # Should set self.deployed_url, which will be reported in the success message.
        # DEV: Get region before building deployed URL.
        self.deployed_url = f"https://{ dsd_config.deployed_project_name }.osc-fr1.scaling.io"

    def _show_success_message(self):
        """After a successful run, show a message about what to do next.

        Describe ongoing approach of commit, push, migrate.
        """
        if dsd_config.automate_all:
            msg = platform_msgs.success_msg_automate_all(self.deployed_url)
        else:
            msg = platform_msgs.success_msg(log_output=dsd_config.log_output)
        plugin_utils.write_output(msg)
