from __future__ import annotations

from pathlib import Path, PosixPath
from typing import List, Sequence, Set, Tuple, Union

import aiohttp

from darwin.dataset.upload_manager import LocalFile
from darwin.datatypes import PathLike
from darwin.future.core.client import ClientCore
from darwin.future.core.items.uploads import async_upload_file
from darwin.future.data_objects.item import ItemCreate, ItemSlot
from darwin.future.exceptions import DarwinException
from darwin.future.meta.objects.item import Item


class UploadItem:
    def __init__(self, name: str, path: str, slots: List[ItemSlot]):
        self.name = name
        self.path = path
        self.slots = slots

    name: str
    path: str
    slots: List[ItemSlot]


def _get_item_path(file: Path, root_path: Path, imposed_path: str, preserve_folders: bool) -> str:
    """
    (internal) Returns the parent path a file should be stored at on the server

    ex. file = /this/path/1.jpg
        root_path = /this
        imposed_path = /location
        preserve_folders = True
        returns /location/path/

    ex. file = /this/path/1.jpg
        root_path = /this
        imposed_path = /location
        preserve_folders = False
        returns /

    Parameters
    ----------
    file: Path
        The file to upload
    root_path: Path
        The path to the (relative) lowest root of the files.
        - Must be a folder path
        - Must be a parent of all files
    imposed_path: str
        The path to impose on the files on the server
        e.g. a file at ./this/path/1.jpg with imposed path of /location
        will go to /location/1.jpg, or /location/this/path/1.jpg if
        preserve_folders is True
    preserve_folders: bool
        Whether to preserve the folder structure when uploading

    Returns
    -------
    str
        The path to the file on the server
    """
    try:
        # Seems counterintuitive, but this is for _remote_ storage, which is POSIX type
        PosixPath(imposed_path).resolve()
    except Exception as exc:
        raise ValueError("imposed_path must be a valid PosixPath") from exc

    assert root_path.is_dir(), "root_path must be a directory"
    assert file.is_absolute() and file.is_file() and file.exists(), "file must be an absolute path to an existing file"

    relative_path = file.relative_to(root_path).parent
    path = Path(imposed_path) / relative_path if preserve_folders else Path(imposed_path)

    return_path = str(path)
    if return_path == ".":
        return "/"

    if not return_path.startswith("/"):
        return_path = "/" + return_path

    return return_path


async def _prepare_upload_items(
    imposed_path: str,
    root_path: Path,
    file_paths: List[Path],
    as_frames: bool,
    fps: int,
    extract_views: bool,
    preserve_folders: bool,
) -> List[UploadItem]:
    """
    (internal) Prepares a list of files to be uploaded

    Arguments
    ---------
    imposed_path: str
        The path to impose on the files on the server
        e.g. a file at ./this/path/1.jpg with imposed path of /location
        will go to /location/1.jpg, or /location/this/path/1.jpg if
        preserve_folders is True
    path: Path
        The path to the (relative) lowest root of the files.
        - Must be a folder path
        - Must be a parent of all files
    file_paths: List[Path]
        The actual files to upload.  These must be absolute, existing objects
        on the local filesystem
    as_frames: bool
        Whether to upload video files as frames
    fps: int
        Frames per second for video files
    extract_views: bool
        Whether to extract views from video files
    preserve_folders: bool
        Whether to preserve the folder structure when uploading

    Returns
    -------
    List[UploadItem]
        The files as UploadItems

    """
    assert root_path.is_dir(), "root_path must be a directory"
    assert all(
        file.is_absolute() and file.is_file() and file.exists() for file in file_paths
    ), "file_paths must be absolute paths"

    return [
        UploadItem(
            name=file.name,
            path=_get_item_path(file, root_path, imposed_path, preserve_folders),
            slots=[
                ItemSlot(
                    slot_name=str(index),
                    file_name=file.name,
                    as_frames=as_frames,
                    fps=fps,
                    extract_views=extract_views,
                )
            ],
        )
        for index, file in enumerate(file_paths)
    ]


def _derive_root_path(paths: List[Path]) -> Tuple[Path, Path]:
    """
    Finds the lowest common path in a set of paths

    Arguments
    ---------
    files: Sequence[Union[PathLike, LocalFile]]
        The set of paths to search

    Returns
    -------
    Tuple[Path, Path]
        The lowest common path to the current working directory, both as a relative path, and an absolute path
    """
    try:
        assert all(isinstance(path, Path) for path in paths), "paths must be a list of Path objects"
    except AssertionError as exc:
        raise ValueError("paths must be a list of Path objects") from exc

    root_path = paths[0]

    for path in paths:
        if len(path.parts) < len(root_path.parts):
            root_path = path

    return Path(root_path.stem), Path(root_path.stem).resolve()


async def _upload_file_to_signed_url(self, url: str, file: Path) -> aiohttp.ClientResponse:
    """
    Uploads a file to a signed URL

    Arguments
    ---------
    url: str
        The signed URL to upload to
    file: PathLike
        The file to upload
    """
    upload = await async_upload_file(self.client, url, file)

    if not upload.ok:
        raise DarwinException(f"Failed to upload file {file} to {url}", upload)

    return upload


async def _create_list_of_all_files(files_to_upload: Sequence[Path], files_to_exclude: Sequence[Path]) -> List[Path]:
    """
    (internal) Creates a flat list of all files to upload from a list of files or file paths and a
    list of files or file paths to exclude

    Arguments
    ---------
    files_to_upload: Sequence[Path]
        The files to upload
    files_to_exclude: Sequence[Path]
        The files to exclude from the upload

    Returns
    -------
    List[Path]
        The list of files to upload
    """
    master_files_to_upload: Set[Path] = set()

    for file in files_to_upload:
        files = file.glob("**/*") if file.is_dir() else [file]
        master_files_to_upload.update(files)

    for file in files_to_exclude:
        files = file.glob("**/*") if file.is_dir() else [file]
        master_files_to_upload.difference_update(files)

    return list(master_files_to_upload)


async def combined_uploader(
    client: ClientCore,
    dataset_id: int,
    item_payload: ItemCreate,
) -> List[Item]:
    """
    (internal) Uploads a list of files to a dataset

    Parameters
    ----------
    client : Client
        The client to use to make the request
    dataset : Dataset
        The dataset to upload the files to
    item_payload : ItemCreate
        The item payload to create the item with.
    use_folders : bool
    """
    files_to_upload = item_payload.files
    files_to_exclude = item_payload.files_to_exclude
    imposed_path = item_payload.path

    files_to_upload = await _create_list_of_all_files(files_to_upload, files_to_exclude)
    # 1. Derive paths

    # 2. Prepare upload items
    # 3. Register and create signed upload url
    # 4. Create list of ItemUploads
    # 5. Upload files
    # 6. Update ItemUploads
    # 7. Confirm upload
    # 8. Update ItemUploads
    # 9. Send results to callback
    # 10. Await completion polling
    # 11. Send results to callback
    # 9. Return ItemUploads

    ...


# async def upload_files_async(
#     team_slug: str,
#     dataset: WFDatasetCore,
#     files: Sequence[PathLike],
#     files_to_exclude: Optional[List[PathLike]] = None,
#     fps: int = 1,
#     path: Optional[str] = None,
#     as_frames: bool = False,
#     extract_views: bool = False,
#     preserve_folders: bool = False,
# ) -> None:
#     """
#     Uploads files to a dataset and optionally starts the workflow

#     Arguments
#     ---------
#     files: Sequence[Union[PathLike, LocalFile]]
#         The files to upload
#     files_to_exclude: Optional[List[PathLike]]
#         Files to exclude from the upload
#     fps: int
#         Frames per second for video files
#     path: Optional[str]
#         Path to upload the files to
#     as_frames: bool
#         Whether to upload video files as frames
#     extract_views: bool
#         Whether to extract views from video files
#     preserve_folders: bool
#         Whether to preserve the folder structure when uploading
#     auto_push: bool
#         Whether to automatically push the files to the next stage

#     Returns
#     -------
#     List[FilesToStatus]
#         A list of files and their upload status
#     """
#     assert team_slug is not None
#     assert dataset.name is not None

#     file_paths = await _convert_filelikes_to_paths(files)
#     if files_to_exclude is not None:
#         file_paths = [f for f in file_paths if f not in files_to_exclude]

#     root_path, root_paths_absolute = _derive_root_path(file_paths)

#     upload_items = await _prepare_upload_items(
#         path or "/", root_path, file_paths, as_frames, fps, extract_views, preserve_folders
#     )

#     items_and_paths: List[Tuple[UploadItem, Path]] = (
#         list(zip(upload_items, [root_paths_absolute])) if preserve_folders else list(zip(upload_items, [Path("/")]))
#     )

#     # This is one API call, so we can't create a multiple output for it
#     upload_urls, upload_ids = await async_register_and_create_signed_upload_url(
#         ClientCore(self.client.config),
#         team_slug,
#         dataset.name,
#         items_and_paths,
#     )

#     assert len(upload_urls) == len(items_and_paths), "Upload info and items and paths should be the same length"

#     updateables = [
#         self._upload_updateable(team_slug, upload_url, upload_id, file.absolute(), auto_push)()
#         for upload_url, upload_id, file in zip(upload_urls, upload_ids, file_paths)
#     ]

#     return updateables, None
