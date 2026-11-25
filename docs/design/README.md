# Design notes

Comments in the code often name a document, such as `decision-layer-zoom-range.md §5`. Those are the written
decisions Talus is built from: each significant feature gets one before any code is written.

The working documents stay in the private repository. They also hold things that don't belong in public, such as
customer names, server addresses, and how past security problems were found. The notes in this folder are public
versions written for readers outside the project. Each one keeps the name and the section numbers of the document
it comes from, so a reference in a comment leads to the same place here.

A note says what was decided, why, and what was turned down. It doesn't repeat the user guide: for how to use a
feature, see the guides in [`docs/`](../).

| Note | What it covers |
|---|---|
| [decision-layer-zoom-range.md](decision-layer-zoom-range.md) | The zooms a layer is drawn at: a layer default and a map's own range |

More are added with each release, starting with the ones the code cites most. If a comment cites a document that
isn't here and the comment alone doesn't explain the code, please open an issue.
