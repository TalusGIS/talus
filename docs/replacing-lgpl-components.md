# Replacing the LGPL components of the Talus GIS api image

The `talus-api` image includes libraries licensed under the GNU Lesser General Public License (LGPL). The LGPL
gives you the right to modify those libraries and to use your modified versions with Talus GIS. This page shows
how, for each one.

Section 2 of the image's notices lists every LGPL component with its exact version and source. Read them inside
the image at `/usr/share/doc/talus/NOTICES`, or as `talus-api-<version>-NOTICES.txt` attached to the release. The
complete source of each component, at the exact version in the image, is attached to the same GitHub Release as
`talus-api-<version>-third-party-source.tar`.

| Component | License | Where it is in the image | How to replace it |
|---|---|---|---|
| psycopg, psycopg-binary, psycopg-pool | LGPL-3.0 | Python packages | Route 2: rebuild with your version |
| GEOS (`libgeos`, `libgeos_c`) | LGPL-2.1-or-later | shared libraries in `rasterio.libs/` | Route 1: replace the files |
| libxcrypt (`libcrypt`) | LGPL-2.1-or-later | shared library in `psycopg_binary.libs/` | Route 1 |
| keyutils (`libkeyutils`) | LGPL-2.1-or-later | shared library in `psycopg_binary.libs/` | Route 1 |
| libquadmath | LGPL-2.1-or-later | shared library in `numpy.libs/` | Route 1 |
| GNU libiconv | LGPL-2.1-or-later | statically linked into lxml's extension modules | Route 2: rebuild lxml |

All paths below are under `/usr/local/lib/python3.12/site-packages/`. The released image is `linux/amd64`.

## Route 1: replace a shared library in the released image

Python wheels bundle their native libraries under hashed file names, such as
`rasterio.libs/libgeos_c-0be36666.so.1.20.5`, and the other libraries in the wheel load them by those exact names.
To use your own build, put it at that name. You do not need to rebuild the image: start from the released image.

1. Build your modified library for Linux. The image is Debian 12 (glibc 2.36), so build on Debian 12 or on
   anything with an older glibc. For GEOS, from the source in the release asset:

   ```
   cmake -S geos-3.14.1 -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DBUILD_TESTING=OFF
   cmake --build build -j
   ```

2. Write a Dockerfile beside your built files. It finds the hashed names itself. For GEOS:

   ```
   FROM ghcr.io/talusgis/talus-api@sha256:<the digest your docker-compose.yml pins>
   USER root
   COPY libgeos.so.3.14.1 libgeos_c.so.1.20.5 /tmp/new/
   RUN set -eu; pip install --no-cache-dir patchelf; \
       L=/usr/local/lib/python3.12/site-packages/rasterio.libs; \
       G=$(basename "$(ls "$L"/libgeos-*.so.3.14.1)"); GC=$(basename "$(ls "$L"/libgeos_c-*.so.1.20.5)"); \
       patchelf --replace-needed libgeos.so.3.14.1 "$G" --set-rpath '$ORIGIN' /tmp/new/libgeos_c.so.1.20.5; \
       patchelf --set-rpath '$ORIGIN' /tmp/new/libgeos.so.3.14.1; \
       install -m 0755 /tmp/new/libgeos.so.3.14.1 "$L/$G"; \
       install -m 0755 /tmp/new/libgeos_c.so.1.20.5 "$L/$GC"; \
       rm -rf /tmp/new; pip uninstall -y patchelf
   USER talus
   ```

   `patchelf` makes your `libgeos_c` look for `libgeos` under the bundled name. For a library that loads no other
   bundled library, a copy is enough. For example, for keyutils:

   ```
   RUN set -eu; L=/usr/local/lib/python3.12/site-packages/psycopg_binary.libs; \
       install -m 0755 /tmp/new/libkeyutils.so.1 "$L/$(basename "$(ls "$L"/libkeyutils-*.so.1.5)")"
   ```

   libxcrypt (`psycopg_binary.libs/libcrypt-*.so.1`) and libquadmath (`numpy.libs/libquadmath-*.so.0.0.0`) work
   the same way. A replacement must keep the library's interface (its soname and symbol versions).

3. Build it with `docker build -t my-talus-api .`, then use it (see "Using your image").

The notices inside an image made this way still describe the released libraries, not yours.

## Route 2: rebuild the image with your modified components

The image is built from `api/Dockerfile` in the release. Make your change there, then build with
`--build-arg TALUS_THIRD_PARTY_CHECK=report`:

```
docker build --build-arg TALUS_THIRD_PARTY_CHECK=report -t my-talus-api api/
```

When the image is built, it checks its third-party components against the ones Talus GIS released and stops if
any differ. With `TALUS_THIRD_PARTY_CHECK=report` it lists the differences and continues instead, and its notices
then start with **MODIFIED BUILD** followed by those differences.

### psycopg

Change the `psycopg[binary,pool]` line in `api/requirements.txt` to your version, or to the path of a wheel you
built and copied into `api/`, then build as above.

### GNU libiconv (statically linked into lxml)

libiconv is compiled into lxml's extension modules, so replacing it means rebuilding lxml 6.1.3 from its source
(`lxml-6.1.3.tar.gz`, in the release asset) against your libiconv:

```
tar -xzf lxml-6.1.3.tar.gz && mkdir -p lxml-6.1.3/libs
cp /path/to/your/libiconv-1.18.tar.gz lxml-6.1.3/libs/
# lxml's build checks each library tarball against a checksum list; give it yours
NEW=$(sha256sum lxml-6.1.3/libs/libiconv-1.18.tar.gz | cut -d' ' -f1)
sed -i "s/^ *[0-9a-f]\{64\}  libiconv-1.18.tar.gz/    $NEW  libiconv-1.18.tar.gz/" lxml-6.1.3/buildlibxml.py
cd lxml-6.1.3 && CFLAGS="-O2 -fPIC" STATIC_DEPS=true LIBICONV_VERSION=1.18 LIBXML2_VERSION=2.14.6 \
  LIBXSLT_VERSION=1.1.43 ZLIB_VERSION=1.3.2 pip wheel --no-deps -w ../wheels .
```

Build the wheel on Linux for the image's platform: inside a `python:3.12-slim-bookworm` container with
`build-essential` installed does it. lxml's build downloads libxml2, libxslt and zlib itself; `-fPIC` is
required because it links them statically into a shared module. Then copy the wheel
into `api/`, point the `lxml` requirement in `api/requirements.txt` at it, and build the image as above.

## Using your image

In `docker-compose.yml`, change the `api` service's `image:` line to your image (for example
`image: my-talus-api`), then run `docker compose up -d api`.
