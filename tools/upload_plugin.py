#!/usr/bin/env python3
"""Upload a built plugin zip to plugins.qgis.org.

    OSGEO_USERNAME=you OSGEO_PASSWORD=... python tools/upload_plugin.py dist/x.zip

This replaces the ``plugin_upload.py`` that QGIS Plugin Builder generated in 2020,
which used ``xmlrpclib`` over plain HTTP and so sent the OSGeo password in clear
text.  The endpoint is HTTPS only here, and credentials come from the environment
rather than from command-line flags that would land in shell history.

The *first* upload of a new plugin must be done through the web form at
https://plugins.qgis.org/plugins/add/ -- the XML-RPC endpoint only accepts new
versions of a plugin that already exists.
"""

from __future__ import annotations

import argparse
import os
import sys
import xmlrpc.client

ENDPOINT = "https://{user}:{password}@plugins.qgis.org:443/plugins/RPC2/"
PUBLIC_ENDPOINT = "https://plugins.qgis.org/plugins/RPC2/"


def upload(path: str, username: str, password: str) -> int:
    if not os.path.exists(path):
        print(f"No such file: {path}", file=sys.stderr)
        return 2

    with open(path, "rb") as handle:
        payload = xmlrpc.client.Binary(handle.read())

    address = ENDPOINT.format(user=username, password=password)
    server = xmlrpc.client.ServerProxy(address, verbose=False)
    print(f"Uploading {os.path.basename(path)} to {PUBLIC_ENDPOINT} as {username}")

    try:
        plugin_id, version_id = server.plugin.upload(payload)
    except xmlrpc.client.ProtocolError as error:
        # Never echo the URL back: it carries the password.
        print(f"Upload rejected: HTTP {error.errcode} {error.errmsg}", file=sys.stderr)
        return 1
    except xmlrpc.client.Fault as error:
        print(f"Upload rejected: {error.faultString}", file=sys.stderr)
        if "already exists" in (error.faultString or ""):
            print(
                "Bump the version in metadata.txt and __init__.py, then retry.",
                file=sys.stderr,
            )
        return 1
    except OSError as error:
        print(f"Could not reach plugins.qgis.org: {error}", file=sys.stderr)
        return 1

    print(f"Uploaded: plugin id {plugin_id}, version id {version_id}")
    print(f"https://plugins.qgis.org/plugins/{plugin_id}/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("zip", help="the plugin zip to upload")
    args = parser.parse_args(argv)

    username = os.environ.get("OSGEO_USERNAME")
    password = os.environ.get("OSGEO_PASSWORD")
    if not username or not password:
        print(
            "Set OSGEO_USERNAME and OSGEO_PASSWORD in the environment.\n"
            "These are your OSGeo web account credentials, the same ones used at "
            "https://plugins.qgis.org/.",
            file=sys.stderr,
        )
        return 2

    return upload(args.zip, username, password)


if __name__ == "__main__":
    raise SystemExit(main())
