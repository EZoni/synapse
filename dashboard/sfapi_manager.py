import json
import os
import re
import time
import asyncio

import requests
from iri_client import Client as IriClient
from trame.widgets import vuetify3 as vuetify

from state_manager import state
from error_manager import add_error

# IRI API resource ID for Perlmutter
PERLMUTTER_RESOURCE_ID = "perlmutter"

# Terminal job states in the IRI/PSIJ model
TERMINAL_JOB_STATES = {"completed", "failed", "canceled"}


def create_iri_client():
    """Create an IRI client using the stored access token and base URL."""
    return IriClient(
        base_url=state.iri_base_url,
        access_token=state.iri_access_token,
    )


def poll_task(client, task_id, timeout=120, interval=2):
    """Poll a task until it reaches a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = json.loads(
            client.call_operation(
                "getTask",
                path_params_json=json.dumps({"task_id": task_id}),
            )
        )
        status = response.get("status", "")
        if status in ("completed", "failed", "canceled"):
            return response
        time.sleep(interval)
    raise TimeoutError(f"Task {task_id} did not complete within {timeout}s")


def monitor_iri_job(client, resource_id, job_id, state_variable):
    """Poll a job until it reaches a terminal state (blocking)."""
    while True:
        response = json.loads(
            client.call_operation(
                "getJob",
                path_params_json=json.dumps(
                    {
                        "resource_id": resource_id,
                        "job_id": job_id,
                    }
                ),
            )
        )
        job_status = response.get("status", {})
        job_state = job_status.get("state", "unknown")
        # Make the status more readable
        readable_status = job_state.replace("_", " ").title()
        if state[state_variable] != readable_status:
            state[state_variable] = readable_status
            state.flush()
            print("Job status: ", state[state_variable])
        if job_state in TERMINAL_JOB_STATES:
            return job_state == "completed"
        time.sleep(5)


async def monitor_iri_job_async(client, resource_id, job_id, state_variable):
    """Async wrapper around monitor_iri_job."""
    return await asyncio.to_thread(
        monitor_iri_job, client, resource_id, job_id, state_variable
    )


def parse_sbatch_script(script_content):
    """Parse a SLURM batch script and convert it to a PSIJ JobSpec dict.

    Extracts #SBATCH directives into structured JobSpec fields and puts
    the remaining script content into the pre_launch field.
    """
    job_spec = {
        "resources": {},
        "attributes": {
            "custom_attributes": {},
        },
    }

    script_body_lines = []

    for line in script_content.splitlines():
        stripped = line.strip()

        # Parse #SBATCH directives
        if stripped.startswith("#SBATCH"):
            directive = stripped[len("#SBATCH") :].strip()

            # Time limit: -t HH:MM:SS or --time=HH:MM:SS
            match = re.match(r"(?:-t|--time[= ])(\S+)", directive)
            if match:
                time_str = match.group(1)
                parts = time_str.split(":")
                if len(parts) == 3:
                    h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                    job_spec["attributes"]["duration"] = h * 3600 + m * 60 + s
                elif len(parts) == 2:
                    m, s = int(parts[0]), int(parts[1])
                    job_spec["attributes"]["duration"] = m * 60 + s
                continue

            # Node count: -N NN or --nodes=NN
            match = re.match(r"(?:-N|--nodes[= ])(\d+)", directive)
            if match:
                job_spec["resources"]["node_count"] = int(match.group(1))
                continue

            # Job name: -J name or --job-name=name
            match = re.match(r"(?:-J|--job-name[= ])(\S+)", directive)
            if match:
                job_spec["name"] = match.group(1)
                continue

            # Account: -A account or --account=account
            match = re.match(r"(?:-A|--account[= ])(\S+)", directive)
            if match:
                job_spec["attributes"]["account"] = match.group(1)
                continue

            # Queue/partition: -q queue or --qos=queue or -p partition
            match = re.match(r"(?:-q|--qos[= ]|-p|--partition[= ])(\S+)", directive)
            if match:
                job_spec["attributes"]["queue_name"] = match.group(1)
                continue

            # Constraint: --constraint=X or -C X
            match = re.match(r"(?:-C|--constraint[= ])(\S+)", directive)
            if match:
                job_spec["attributes"]["custom_attributes"]["constraint"] = match.group(
                    1
                )
                continue

            # Tasks per node: --ntasks-per-node=N
            match = re.match(r"--ntasks-per-node[= ](\d+)", directive)
            if match:
                job_spec["resources"]["processes_per_node"] = int(match.group(1))
                continue

            # GPUs per node: --gpus-per-node=N
            match = re.match(r"--gpus-per-node[= ](\d+)", directive)
            if match:
                job_spec["resources"]["gpu_cores_per_process"] = int(match.group(1))
                continue

            # Stdout: -o path or --output=path
            match = re.match(r"(?:-o|--output[= ])(\S+)", directive)
            if match:
                job_spec["stdout_path"] = match.group(1)
                continue

            # Stderr: -e path or --error=path
            match = re.match(r"(?:-e|--error[= ])(\S+)", directive)
            if match:
                job_spec["stderr_path"] = match.group(1)
                continue

        # Skip shebang and comment-only SBATCH lines
        elif stripped.startswith("#!"):
            continue

        # Everything else goes to pre_launch
        else:
            script_body_lines.append(line)

    # Set the script body as pre_launch
    pre_launch = "\n".join(script_body_lines).strip()
    if pre_launch:
        job_spec["pre_launch"] = pre_launch

    # Clean up empty dicts
    if not job_spec["attributes"].get("custom_attributes"):
        job_spec["attributes"].pop("custom_attributes", None)
    if not job_spec.get("attributes"):
        job_spec.pop("attributes", None)
    if not job_spec.get("resources"):
        job_spec.pop("resources", None)

    return job_spec


def upload_file_to_nersc(resource_id, target_dir, local_path, filename=None):
    """Upload a file to NERSC via the IRI API upload endpoint.

    Uses the requests library directly since iri_client doesn't support
    multipart form data uploads.
    """
    base_url = state.iri_base_url or "https://api.iri.nersc.gov"
    access_token = state.iri_access_token

    url = f"{base_url}/api/v1/filesystem/upload/{resource_id}"
    headers = {"Authorization": access_token}

    if filename is None:
        filename = os.path.basename(str(local_path))

    with open(local_path, "rb") as f:
        files = {"file": (filename, f)}
        params = {"path": target_dir}
        response = requests.post(url, headers=headers, files=files, params=params)
        response.raise_for_status()

    # Poll the upload task to completion
    result = json.loads(response.text)
    task_id = result.get("task_id")
    if task_id:
        client = create_iri_client()
        poll_task(client, task_id)

    return result


def parse_iri_credentials(key_str):
    """Parse IRI access token from an uploaded file.

    Accepts:
    - A single-line file containing just the access token.
    - The old sfapi key file format (client_id on first line, PEM key following):
      in this case a ValueError is raised because PEM keys are not compatible
      with the IRI API.
    """
    key_lines = key_str.splitlines()
    first_line = key_lines[0].rstrip()

    # Check if this looks like an old PEM key file
    if (
        len(key_lines) > 1
        and key_lines[1].rstrip() == "-----BEGIN RSA PRIVATE KEY-----"
    ):
        state.sfapi_client_id = first_line
        raise ValueError(
            "PEM key files from sfapi_client are not directly compatible with the "
            "IRI API. Please set the IRI_ACCESS_TOKEN environment variable or upload "
            "a file containing only the IRI access token."
        )

    # Otherwise, treat as an IRI access token
    state.iri_access_token = first_line


def initialize_iri():
    """Initialize the IRI API connection."""
    print("Initializing IRI API...")
    # Check for access token in environment
    access_token = os.getenv("IRI_ACCESS_TOKEN")
    base_url = os.getenv("IRI_BASE_URL")

    if access_token:
        state.iri_access_token = access_token
    if base_url:
        state.iri_base_url = base_url

    # Also try to load a token file
    token_path = os.path.join(os.getcwd(), "iri_token.txt")
    if os.path.isfile(token_path) and not state.iri_access_token:
        try:
            with open(token_path, "r") as f:
                token = f.read().strip()
            if token:
                state.iri_access_token = token
                print("Loaded IRI access token from iri_token.txt")
        except Exception as e:
            print(f"Warning: Could not read token file: {e}")

    # Try backward-compatible key file
    key_path = os.path.join(os.getcwd(), "priv_key.pem")
    if os.path.isfile(key_path) and not state.iri_access_token:
        try:
            with open(key_path, "r") as f:
                key_str = f.read()
            parse_iri_credentials(key_str)
        except ValueError as e:
            print(f"Warning: {e}")
        except Exception as e:
            print(f"Warning: Could not parse key file: {e}")

    # If we have a token, try to connect
    if state.iri_access_token:
        try:
            update_iri_info()
        except Exception as e:
            title = "Unable to initialize the IRI API connection"
            msg = f"Error occurred when initializing the IRI API connection: {e}"
            add_error(title, msg)
            print(msg)
    else:
        print(
            "No IRI access token found. "
            "Set IRI_ACCESS_TOKEN environment variable or upload a token file."
        )


def update_iri_info():
    """Update Perlmutter status using the IRI API."""
    print("Updating IRI API info...")
    try:
        client = create_iri_client()

        # Query Perlmutter resource status
        response = json.loads(
            client.call_operation(
                "getResources",
                query_json=json.dumps({"name": "perlmutter"}),
            )
        )

        # response is a list of resources
        if response and len(response) > 0:
            perlmutter = response[0]
            status = perlmutter.get("current_status", "unknown")
            description = perlmutter.get("description", "")
            state.perlmutter_description = description or "Available"
            state.perlmutter_status = status or "unknown"
            state.sfapi_key_expiration = "Connected"
            print(
                f"Perlmutter status is {state.perlmutter_status} "
                f"with description '{state.perlmutter_description}'"
            )
        else:
            state.perlmutter_description = "Resource not found"
            state.perlmutter_status = "unknown"
            print("Perlmutter resource not found in IRI API response")
    except Exception as e:
        print(f"An unexpected error occurred when connecting to IRI API:\n{e}")
        state.sfapi_key_expiration = "Unavailable"
        state.perlmutter_description = "Unavailable"
        state.perlmutter_status = "unavailable"
        title = "Unable to connect to NERSC"
        msg = f"Error occurred when connecting to NERSC through the IRI API: {e}"
        add_error(title, msg)
        print(msg)


@state.change("sfapi_key_dict")
def load_iri_credentials(**kwargs):
    """Handle uploaded credential file."""
    # skip if triggered on server ready (all state variables marked as modified)
    if len(state.modified_keys) == 1:
        if state.sfapi_key_dict is not None:
            print("Loading IRI credentials...")
            key_str = state.sfapi_key_dict["content"].decode("utf-8")
            try:
                parse_iri_credentials(key_str)
                update_iri_info()
            except ValueError as e:
                title = "Invalid credential file"
                msg = str(e)
                add_error(title, msg)
                print(msg)
            except Exception as e:
                title = "Unable to load IRI credentials"
                msg = f"Error occurred when loading IRI credentials: {e}"
                add_error(title, msg)
                print(msg)


def load_iri_card():
    """Load the IRI API card in the UI."""
    print("Setting IRI API card...")
    with vuetify.VCard():
        with vuetify.VCardTitle("NERSC IRI API"):
            with vuetify.VCardText():
                # row with component to upload token file
                with vuetify.VRow():
                    vuetify.VFileInput(
                        v_model=("sfapi_key_dict",),
                        label="Access Token File (text file with IRI access token)",
                    )
                # row with text field to display connection status
                with vuetify.VRow():
                    with vuetify.VCol():
                        vuetify.VTextField(
                            v_model=("sfapi_key_expiration",),
                            label="Connection Status",
                            readonly=True,
                        )
                # row with text field to display Perlmutter status
                with vuetify.VRow():
                    with vuetify.VCol():
                        vuetify.VTextField(
                            v_model=("perlmutter_description",),
                            label="Perlmutter Status",
                            readonly=True,
                        )
