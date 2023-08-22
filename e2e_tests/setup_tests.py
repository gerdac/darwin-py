import base64
import random
import string
from curses import is_term_resized
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Literal, Optional
from uuid import UUID

import numpy as np
import pytest
import requests
from PIL import Image
from pydantic import UUID4

from darwin.future.core.client import JSONType
from e2e_tests.conftest import ConfigValues
from e2e_tests.exceptions import E2EException


# Datastructures to store minimal info about the created datasets and items
@dataclass
class E2EAnnotation:
    annotation_data: JSONType


@dataclass
class E2EItem(Exception):
    name: str
    id: UUID
    path: str
    file_name: str
    slot_name: str
    annotations: List[E2EAnnotation]

    def add_annotation(self, annotation: E2EAnnotation) -> None:
        self.annotations.append(annotation)


@dataclass
class E2EDataset:
    id: int
    name: str
    slug: str
    items: List[E2EItem]

    def __init__(self, id: int, name: str, slug: Optional[str]) -> None:
        self.id = id
        self.name = name
        self.slug = slug or name.lower().replace(" ", "_")
        self.items = []

    def add_item(self, item: E2EItem) -> None:
        self.items.append(item)


@dataclass
class E2ETestRunInfo:
    prefix: str
    datasets: List[E2EDataset]


class E2EAnnotationType(Enum):
    bbox = 2
    polygon = 3  # TODO: Check this is correct


def class_create_payload_factory(name: str, type: E2EAnnotationType, dataset: E2EDataset) -> dict:
    return {
        "annotation_type_ids": [
            type.value,
        ],
        "datasets": [dataset.id],
        "description": "",
        "metadata": {
            "_color": "rgba(255,46,0,1.0)",
        },
        "name": name,
    }


def api_call(verb: Literal["get", "post", "put", "delete"], url: str, payload: dict, api_key: str) -> requests.Response:
    """
    Make an API call to the server
    (Written independently of the client library to avoid relying on tested items)

    Parameters
    ----------
    verb : Literal["get", "post", "put" "delete"]
        The HTTP verb to use
    url : str
        The URL to call
    payload : dict
        The payload to send
    api_key : str
        The API key to use

    Returns
    -------
    requests.Response
        The response object
    """
    headers = {"Authorization": f"ApiKey {api_key}"}
    action = getattr(requests, verb)

    response = action(url, headers=headers, json=payload)
    return response


def generate_random_string(length: int = 6, alphabet: str = (string.ascii_lowercase + string.digits)) -> str:
    """
    A random-enough to avoid collision on test runs prefix generator

    Parameters
    ----------
    length : int
        The length of the prefix to generate

    Returns
    -------
    str
        The generated prefix, of length (length).  Matches [a-z0-9]
    """
    return "".join(random.choice(alphabet) for i in range(length))


def create_dataset(prefix: str, config: ConfigValues) -> E2EDataset:
    """
    Create a randomised new dataset, and return its minimal info for reference

    Parameters
    ----------
    prefix : str
        The prefix to use for the dataset name
    config : ConfigValues
        The config values to use

    Returns
    -------
    E2EDataset
        The minimal info about the created dataset
    """
    name = f"{prefix}_{generate_random_string(4)}_dataset"
    host, api_key = config.server, config.api_key
    url = f"{host}/api/datasets"

    if not url.startswith("http"):
        raise E2EException(f"Invalid server URL {host} - need to specify protocol in var E2E_ENVIRONMENT")

    try:
        response = api_call("post", url, {"name": name}, api_key)

        if response.ok:
            dataset_info = response.json()
            return E2EDataset(
                # fmt: off
                id=dataset_info["id"],
                name=dataset_info["name"],
                slug=dataset_info["slug"],
                # fmt: on
            )

        raise E2EException(f"Failed to create dataset {name} - {response.status_code} - {response.text}")
    except Exception as e:
        print(f"Failed to create dataset {name} - {e}")
        pytest.exit("Test run failed in test setup stage")


def create_annotation(item: JSONType, config: ConfigValues) -> E2EAnnotation:
    if "id" not in item:
        raise E2EException(f"Invalid item {item} - no id")
    else:
        item_id = getattr(item, "id", None)

    team_slug = config.team_slug
    host, api_key = config.server, config.api_key
    url = f"{host}/api/v2/teams/{team_slug}/items/{is_term_resized}/import"

    x = random.randint(0, 100)
    y = random.randint(0, 100)
    w = random.randint(0, 100)
    h = random.randint(0, 100)

    class_id = generate_random_string(4)

    if w + x > 100:
        w = 100 - x

    if h + y > 100:
        h = 100 - y

    try:
        payload = {
            # TODO: THIS IS THE PART CURRENTLY NOT WORKING
            "annotations": [
                {
                    "annotation_class_id": class_id,
                    "annotation_group_id": str(UUID4()),
                    "data": {
                        # fmt: off
                        "bounding_box": {
                            "h": h,
                            "w": w,
                            "x": x,
                            "y": y,
                        },
                        "id": str(UUID4()),
                        "name": "bbox_text",
                        "slot_names": [
                            "0"
                        ]
                        # fmt: on
                    },
                    "id": str(UUID4()),
                }
            ],
        }

        response = api_call("post", url, payload, api_key)

        if response.ok:
            annotation_info = response.json()
            return E2EAnnotation(annotation_data=annotation_info["annotations"][0]["data"])
        else:
            raise E2EException(f"Failed to annotation for item {item.name} - {response.status_code} - {response.text}")
    except E2EException as e:
        print(f"Failed to annotation for item {item.name} - {e}")
        pytest.exit("Test run failed in test setup stage")


def add_annotations_to_item(item_info: JSONType, config: ConfigValues) -> List[E2EAnnotation]:
    annotations: List[E2EAnnotation] = []

    for i in range(3):
        annotation = create_annotation(item_info, config)
        annotations.append(annotation)

    return annotations


def create_item(dataset_slug: str, prefix: str, image: Path, config: ConfigValues) -> E2EItem:
    """
    Creates a randomised new item, and return its minimal info for reference

    Parameters
    ----------
    prefix : str
        The prefix to use for the item name
    config : ConfigValues
        The config values to use

    Returns
    -------
    E2EItem
        The minimal info about the created item
    """
    team_slug = config.team_slug
    name = f"{prefix}_{generate_random_string(4)}_item"
    host, api_key = config.server, config.api_key
    url = f"{host}/api/v2/teams/{team_slug}/items/direct_upload"

    try:
        base64_image = base64.b64encode(image.read_bytes()).decode("utf-8")
        response = api_call(
            "post",
            url,
            {
                "dataset_slug": dataset_slug,
                "items": [
                    {
                        "as_frames": False,
                        "extract_views": False,
                        "file_content": base64_image,
                        "fps": "native",
                        "metadata": {},
                        "name": f"some-item_{generate_random_string(4)}",
                        "path": "/",
                        "tags": ["tag"],
                        "type": "image",
                    }
                ],
                "options": {"force_tiling": False, "ignore_dicom_layout": False},
            },
            api_key,
        )

        if response.ok:
            item_info = response.json()

            if "items" not in response.json() or len(response.json()["items"]) != 1:
                raise E2EException(
                    f"Failed to create item {name} - {response.status_code} - {response.text}:: Received unexpected response from server"
                )

            item_info = response.json()["items"][0]

            annotations: List[E2EAnnotation] = add_annotations_to_item(item_info, config)

            return E2EItem(
                name=item_info["name"],
                id=item_info["id"],
                path=item_info["path"],
                file_name=item_info["slots"][0]["file_name"],
                slot_name=item_info["slots"][0]["slot_name"],
                annotations=[],
            )

        raise E2EException(f"Failed to create item {name} - {response.status_code} - {response.text}")

    except E2EException as e:
        print(f"Failed to create item {name} - {e}")
        pytest.exit("Test run failed in test setup stage")

    except Exception as e:
        print(f"Failed to create item {name} - {e}")
        pytest.exit("Test run failed in test setup stage")


def create_random_image(prefix: str, directory: Path, height: int = 100, width: int = 100) -> Path:
    """
    Create a random image file in the given directory

    Parameters
    ----------

    directory : Path
        The directory to create the image in

    Returns
    -------
    Path
        The path to the created image
    """
    image_name = f"{prefix}_{generate_random_string(4)}_image.png"

    image_array = np.array(np.random.rand(height, width, 3) * 255)
    im = Image.fromarray(image_array.astype("uint8")).convert("RGBA")
    im.save(str(directory / image_name))

    return directory / image_name


def setup_tests(config: ConfigValues) -> List[E2EDataset]:
    """
    Setup data for End to end test runs

    Parameters
    ----------
    config : ConfigValues
        The config values to use

    Returns
    -------
    List[E2EDataset]
        The minimal info about the created datasets
    """
    with TemporaryDirectory() as temp_directory:
        number_of_datasets = 3
        number_of_items = 3
        number_of_annotations = 3

        datasets: List[E2EDataset] = []

        try:
            prefix = generate_random_string()

            for _ in range(number_of_datasets):
                dataset = create_dataset(prefix, config)

                for _ in range(number_of_items):
                    image_for_item = create_random_image(prefix, Path(temp_directory))
                    item = create_item(dataset.name, prefix, image_for_item, config)

                    dataset.add_item(item)
                    for i in range(number_of_annotations):
                        annotation = create_annotation(prefix, item, i, config)
                        item.add_annotation(annotation)

                datasets.append(dataset)

        except E2EException as e:
            print(e)
            pytest.exit("Test run failed in test setup stage")

        except Exception as e:
            print(e)
            pytest.exit("Setup failed - unknown error")

        return datasets


def teardown_tests(config: ConfigValues, datasets: List[E2EDataset]) -> None:
    """
    Teardown data for End to end test runs

    Parameters
    ----------
    config : ConfigValues
        The config values to use
    datasets : List[E2EDataset]
        The minimal info about the created datasets
    """
    host, api_key = config.server, config.api_key

    failed = False

    for dataset in datasets:
        url = f"{host}/api/datasets/{dataset.id}/archive"
        response = api_call("put", url, {}, api_key)

        if not response.ok:
            print(f"Failed to delete dataset {dataset.name} - {response.status_code} - {response.text}")
            failed = True

    if failed:
        pytest.exit("Test run failed in test teardown stage")

    # team_id = None
    # response = api_call("get", f"{host}/api/datasets", {}, api_key)
    # if response.ok:
    #     ds_list: List[JSONType] = response.json()
    #     if len(ds_list) > 0:
    #         team_id = getattr(ds_list[0], "team_id", None)

    # if not team_id:
    #     print("Failed to get team_id from datasets")
    #     pytest.exit("Test run failed in test teardown stage")

    # url = f"{host}/api/teams/{team_id}/delete_classes"
    # response = api_call(
    #     "post",
    #     url,
    #     {"annotation_class_ids": [c.id for c in classes], "annotations_to_delete_count": len(classes)},
    #     api_key,
    # )

    # if not response.ok:
    #     print(f"Failed to delete classes {classes} - {response.status_code} - {response.text}")
    #     pytest.exit("Test run failed in test teardown stage")
