"""Third-party software in the talus-api image: the inventory, the drift check, the NOTICES generator.

Runs at image build (api/Dockerfile) against the tree that actually ships:

    python3 -m third_party_notices check

The image carries its own truth: a change to the third-party content that needs a human — a new bundled
native library, a changed copyleft binary, newly statically linked copyleft code, a new copyleft Python
package — fails the build. Standard library only; it runs in the image's own interpreter.
"""
