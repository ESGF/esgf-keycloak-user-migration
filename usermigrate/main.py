""" Script to run the migration. """

__author__ = "William Tucker"
__date__ = "2020-08-04"
__copyright__ = "Copyright 2020 United Kingdom Research and Innovation"
__license__ = "BSD - see LICENSE file in top-level package directory"


import click
import click_config_file
import importlib
import json
import logging
import os
import time
import urllib3

from enum import Enum
from functools import partial
from multiprocessing import Pool
from sqlalchemy.exc import ProgrammingError
from tqdm.contrib.concurrent import process_map

from usermigrate.db import Connection
from usermigrate.keycloak import KeycloakApi
from usermigrate.keycloak.exceptions import KeycloakAuthenticationError, \
    KeycloakCommunicationError, KeycloakConflictError


LOG = logging.getLogger(__name__)

DEFAULT_USER_MODEL = "usermigrate.db.models.User"


@click.command()
@click.option("-k", "--keycloak_url", required=True,
              help=("The URL of the Keycloak server."))
@click.option("-r", "--keycloak_realm", required=True,
              help="The Keycloak realm ID, e.g. 'master'")
@click.option("-u", "--keycloak_user", required=True,
              help="The Keycloak admin API user.")
@click.option("--keycloak_password", prompt=True, hide_input=True)
@click.option("--cacert", required=False,
              help="Certificate file path for verifying the Keycloak connection.")
@click.option("--insecure", default=False,
              help="Ignore Keycloak server certificate verification.")
@click.option("-f", "--file_input",
              help="Path to a JSON file containing a list of users to import.")
@click.option("-H", "--database_host", default="localhost",
              help="The database host.")
@click.option("-p", "--database_port", default="5432",
              help="The database port.")
@click.option("-d", "--database_name", required=True,
              help="The source database name.")
@click.option("-U", "--database_user",
              help="The database user.")
@click.option("--database_password", prompt=True, hide_input=True)
@click.option("-m", "--user_model", default=DEFAULT_USER_MODEL,
              help=("Python import path to a valid SQLAlchemy model"
                    " representing a Keycloak user."))
@click_config_file.configuration_option()
def main(keycloak_url, keycloak_realm, keycloak_user, keycloak_password,
        cacert, insecure, file_input, database_host, database_port,
        database_name, database_user, database_password, user_model):
    """ Migrates users and groups from a specified database into Keycloak.
    Will not overwrite existing users or groups. """

    # Load the SQLAlchemy user model
    module_name, _, class_name = user_model.rpartition('.')
    user_model_module = importlib.import_module(module_name)
    user_model_class = getattr(user_model_module, class_name)

    # Setup database connection values
    database_connection_data = {
        "user": database_user,
        "password": database_password,
        "host": database_host,
        "port": database_port,
        "database": database_name,
    }

    # Keycloak API connection setup
    verify = not insecure
    if cacert:
        verify = cacert
    keycloak_url = keycloak_url.rstrip('/')
    keycloak_api = KeycloakApi(keycloak_url, keycloak_realm, keycloak_user, \
        keycloak_password, verify=verify)

    # Check Keycloak connection
    print(f"Checking connection to Keycloak server at '{keycloak_url}'")
    try:
        keycloak_api.check_connection()

    except ConnectionError as e:
        LOG.error(("Failed to connect to Keycloak server: {}").format(e))
        return

    # Discover users
    cache_file_path = os.path.abspath("./user_cache")

    if file_input:
        print(f"Reading users from input file {file_input}...")

    elif not file_input and os.path.exists(cache_file_path):

        print(f"Found cached users at {cache_file_path}.")
        response = None
        while response not in ["y", "n"]:
            response = input(("Skip database discovery and import from cache?"
                " y/n\n")).lower()

        if response == "y":
            file_input = cache_file_path

        elif response == "n":
            print("Rediscovering.")

    if not file_input:

        file_input = cache_file_path
        if os.path.exists(cache_file_path):
            print("Removing old cache.")
            os.remove(cache_file_path)

        # Attempt to parse Keycloak-compatible user objects from the database
        print(f"Discovering users from the database...")
        try:
            discover(database_connection_data, user_model_class, cache_file_path)

        except Exception as e:

            print("Cleaning up failed cache")
            os.remove(cache_file_path)
            return

    users = []
    try:
        with open(file_input) as users_file:
            for line in users_file:
                users.append(json.loads(line))
    except Exception as e:
        LOG.error(f"Failed to load users from {file_input}: {e}")
        return

    if not users:
        print("No users found.\nNothing to do.")
        return

    print("Parsing groups from users...")
    groups = {}
    for user in users:
        for group in user["groups"]:
            groups[group] = {"name": group}
    groups = groups.values()

    print(f"{len(users)} users and {len(groups)} unique groups found.")

    # Attempt to populate the Keycloak server with discovered users
    print("Starting import...")
    try:

        with keycloak_api:

            # Suppress redundant insecure request warnings
            if insecure:
                urllib3.disable_warnings(
                    urllib3.exceptions.InsecureRequestWarning)

            import_objects = [
                ("group", "name", groups),
                ("user", "username", users),
            ]
            for object_type, name_key, values in import_objects:
                populate_keycloak(
                    keycloak_api, object_type, values, name_key=name_key)

    except ConnectionError as e:
        LOG.error(("Couldn't connect to Keycloak server '{}'. Error was: {}"
            ).format(keycloak_url, e))
        return

    except KeycloakCommunicationError as e:
        LOG.error(str(e))
        return

    except KeycloakAuthenticationError as e:
        LOG.error(("").format(keycloak_user))
        return


def discover(database_connection_data, user_model_class, cache_file_path):
    """ Discover users from a database. """

    start = time.time()
    try:
        with Connection(**database_connection_data) as connection:

            database_users = connection.load_users(user_model_class)
            for user in database_users:
                cache_object(cache_file_path, user.data)

    except ProgrammingError as e:

        LOG.error("Error connecting to the database: {}".format(str(e)))
        raise e

    except Exception as e:

        LOG.error(f"User discovery failed: {e}")
        raise e

    end = time.time()
    print((f"Database query completed in {int(end - start)} seconds."))
    print(f"Created user cache at {cache_file_path}")


class ImportResult(Enum):

    FAILED = 0
    LOADED = 1
    EXISTS = 2
    SKIPPED = 3


def import_value(value, object_type, name_key, api, log_file_path, retry_cache_path):

    name = value.get(name_key)
    if not name:

        write_log_message(log_file_path,
            f"Name field missing from {value}, skipping")
        cache_object(retry_cache_path, value)

        return ImportResult.SKIPPED

    try:

        success = api.post(object_type, value)
        if success:
            return ImportResult.LOADED

    except KeycloakConflictError:

        write_log_message(log_file_path,
            f"The {object_type} {name} already exists, cannot overwrite.")

        return ImportResult.EXISTS

    except Exception as e:

        write_log_message(log_file_path,
            f"Failed to import {object_type} {value}, error was: {e}")
        cache_object(retry_cache_path, value)

        return ImportResult.FAILED


def populate_keycloak(api, object_type, values, name_key):
    """ Imports a set of Keycloak compatible objects into Keycloak. """

    print(f"Starting {object_type} import.")

    log_file_path = os.path.abspath(f"{object_type}_import.log")
    if os.path.exists(log_file_path):
        print(f"Removing previous log file.")
        os.remove(log_file_path)

    retry_cache_path = os.path.abspath(f"{object_type}_retry_cache")
    if os.path.exists(retry_cache_path):
        print(f"Removing previous retry cache.")
        os.remove(retry_cache_path)

    print(f"Writing errors to {log_file_path}.")

    print(f"Importing {len(values)} {object_type} objects into Keycloak.")

    loop_kwargs = {
        "object_type": object_type,
        "name_key": name_key,
        "api": api,
        "log_file_path": log_file_path,
        "retry_cache_path": retry_cache_path,
    }
    loop_function = partial(import_value, **loop_kwargs)
    results = process_map(loop_function, values, max_workers=8, chunksize=1)

    report = {
        ImportResult.FAILED: 0,
        ImportResult.LOADED: 0,
        ImportResult.EXISTS: 0,
        ImportResult.SKIPPED: 0,
    }
    for result in results:
        report[result] += 1

    failed_count = report[ImportResult.FAILED]
    loaded_count = report[ImportResult.LOADED]
    existing_count = report[ImportResult.EXISTS]
    skipped_count = report[ImportResult.SKIPPED]

    cached_for_retry_count = skipped_count + failed_count
    message = (f"Imported {loaded_count} out of {len(values)}"
        f" {object_type} objects. There were {failed_count} failures.")
    if skipped_count > 0:
        message = (f"{message}\n{skipped_count} {object_type} objects"
            " were skipped because their name field was missing or blank.")
    if existing_count > 0:
        message = (f"{message}\n{existing_count} {object_type} objects"
            " were already in Keycloak and did not get overwritten.")
    if cached_for_retry_count > 0:
        message = (f"{message}\nRerun with '-f {retry_cache_path}' to retry"
            f" {cached_for_retry_count} skipped or failed objects.")
    print(message)
    write_log_message(log_file_path, message)


def write_log_message(log_file_path, message):
    """ Appends a message to the end of a log file. """

    with open(log_file_path, "a") as log_file:
        log_file.write(f"{message}\n")


def cache_object(cache_file_path, object_data):
    """ Write an object dict to a file of a file. """

    add_new_line = False
    if os.path.exists(cache_file_path):
        add_new_line = True

    with open(cache_file_path, "a") as cache_file:

        if add_new_line:
            cache_file.write("\n")
        cache_file.write(json.dumps(object_data))


if __name__ == "__main__":

    main()
