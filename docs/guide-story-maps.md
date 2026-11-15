# Story maps (Talus+)

A story map is a scrolling narrative built on one of your saved web maps. It is a list of steps:
as the reader scrolls to a step's text, the map beside it moves to the view and layers you saved
for that step. Story maps are a Talus+ module and appear only when the deployment's license
includes them; on Talus Community they do not appear at all.

## Before you start

- **Save the web map first.** A story narrates exactly one web map (see `guide-maps.md`).
- **Attach photos to features first.** A story cannot upload images; it shows photos already
  attached to features on that map's layers (see `guide-layers.md`).
- **Share the map and layers with your readers.** Sharing a story shares nothing inside it.
  Readers see only the map, layers and photos shared with them.
- **Check your role.** Only publishers and administrators can create stories. Anyone signed in
  can read a story shared with them; a public story needs no sign-in.

## Where to find it

- **New** menu, top navigation: **Story map** opens a new story.
- **Catalog**, **Stories** tab: **My stories** and **Shared with me**. Administrators also see
  everyone else's stories in the second list.

## What a step can show

| Step content | What the reader sees | What it needs |
|---|---|---|
| Heading and text | A heading and plain paragraphs; each line break starts a new paragraph. | Nothing; both are optional. |
| Map view | The map moves to the step's saved center and zoom, plus rotation and tilt if set. | Every step has one, captured from the editor's map. |
| Layer visibility | Only the ticked layers draw for that step. | Layers on the story's map. If unset, the map's own visibility applies. |
| Image beside the text | The photo appears in the step, under the heading. | A photo attached to a feature on one of the map's layers. |
| Image that fills the map pane | The photo covers the map while the step is active. | As above, with **Fills the map pane** chosen. |
| Before / after swipe | The same map twice at the same view, split by a divider the reader drags: one layer set on the left (**Before**), another on the right (**After**). | Layers on the story's map. A swipe compares layer sets, not two different maps. |

## Create a story

1. Open **New** and choose **Story map**, or open **Catalog**, then **Stories**, and click
   **+ New Story**.
2. Under *A story narrates one map. Pick it first:*, click a map you own or one shared with you.
   With no maps, the page says *No maps available — save a web map first.*
3. Type a **Story title** (required, up to 200 characters) and, if you like, a
   **Short description**. Both show at the top of the story.
4. Pan and zoom the map on the right to where the story begins, then click
   **+ Add step from current view**.
5. Fill in the step (see the next section), and repeat step 4 for each step.
6. Click **Create story**. The button stays disabled until the story has a title and at least one
   step.

After the first save, the button reads **Save**, and a **View** button opens the story as readers
see it. Saving takes effect immediately: there are no drafts, no publish step and no autosave, so
save before you leave the page. Who can read the story is set by sharing.

## Add a step with a map view

1. Move the editor's map to the view you want. If you rotate or tilt it, the step shows
   *Rotated …° · tilted …°* and a **North-up** button that resets it to flat and north-up.
2. Click **+ Add step from current view** for a new step, or **Use current view** on an existing
   step to replace its view.
3. Type a **Step heading (optional)** and the step text. The text is plain: no bold, links, lists
   or other formatting.
4. To control which layers draw, tick **Set layer visibility for this step**, then tick the layers
   to show.
5. Click **Preview** to move the map to the step's saved view, with its layers, swipe and image.
6. Use **↑** and **↓** to reorder steps, and **✕** to delete one. A deleted step is not
   confirmed first; nothing changes until you save.

## Add an image from one of your layers

1. On a step, click **Add image**. The **Choose an image** window opens.
2. In **Layer**, choose one of the story map's layers. The list shows the map's feature layers
   that you can view; a layer marked **(no attachments)** has no photos to offer.
3. If the layer is not public, the window says *This layer isn’t public. Publishing this story
   will ask you to review it.*
4. Photos are grouped by feature, labeled with the layer's display field. To narrow the list, type
   in **Search by**. Click **Show more (… more)** to load more photos.
5. Click a photo to use it. Only image attachments are listed.
6. Under the thumbnail, choose **Beside the text** or **Fills the map pane**. Use
   **Change image** or **Remove image** to change it.

Each step holds one image. There is no caption or alt-text field, so describe the photo in the
step text if it matters.

## Add a swipe comparison

1. On a step, tick **Before / after swipe — a second layer set behind a draggable divider**.
2. The first checkbox now reads **Before — left side: set layer visibility**. Tick it and choose
   the left-side layers. If you leave it unticked, the left side uses the map's own visibility.
3. Under **After — right side**, tick the layers for the right side.
4. Click **Preview** to try the divider.

Readers see **Before** and **After** labels, the hint *Drag the divider on the map to compare.*,
and a slider they can drag, or move with the arrow keys once it has focus.

## Read a story

Open a story from the **Stories** tab or its link. As the reader scrolls, the step crossing the
middle of the text pane becomes active and the map moves to it. The map pane has a bar with the
map's title, **Legend** and **Open full map ↗**. On narrow screens the map sits above the text.

With reduced motion requested by the device, the map jumps instead of flying and the step
highlight does not animate. There are no extra keyboard shortcuts.

## Share a story

On the **Stories** tab, **Edit**, **Share** and **Delete** appear on stories under **My stories**
for publishers and administrators.

1. Click **Share** on the story.
2. Under **Sharing level**, choose **Owner Only**, **Organization Only** or **Public Access**.
   Under **Groups**, tick any groups to share with; the list shows the groups you may share to.
3. Click **Save**.

**Public Access** is available only if an administrator has turned public access on for the
organization (Admin, then **Sharing & access**). If non-administrators are not allowed to share
publicly, only an administrator can choose it.

When you save a public story, Talus checks the story's map and the layers its images come from.
Any that are not public are listed under **Review sharing**. Nothing is made public for you;
share each from its own Manage dialog (see `guide-portal.md`). The review does not check the
map's other layers, so check those yourself.

### What a reader sees when something is not shared

- **The story itself:** *This story isn’t available.*
- **The map:** the story text still shows, but the map pane shows *Could not open this map*,
  followed by an error code.
- **Some of the map's layers:** those layers are left out, and the map bar shows a message such
  as *2 layers in this map are unavailable to you and were skipped.*
- **An image:** it does not appear. There is no placeholder or error.

## Limits, and what story maps deliberately do not do

- One web map per story; steps cannot switch maps.
- Plain text only: no formatting, links or embedded HTML.
- No uploads. Images come only from photos already attached to features on the story's map; no
  video, audio or content from other sites.
- No cover image, captions or alt text.
- No drafts or scheduled publishing: a save is live for everyone the story is shared with.
- Title up to 200 characters, step heading 80, step text 4,000; up to 100 steps.

## If something goes wrong

- **No Story map in New, no Stories tab:** the deployment has no valid license that includes
  story maps. An administrator checks Admin → Talus+ → **License** (`talus-plus-runbook.md`).
- ***Stories aren’t available here.*** The list could not load. Sign in again and reload.
- ***This editor isn’t available.*** Your account is not a publisher or administrator, or the story
  could not be opened.
- **An error saying a step *names a layer not on this story's map*:** a layer was removed from the
  map after a step chose it. In each step that sets layers, change any checkbox in its list (this
  drops the missing layer) or untick the list's checkbox, then save again.
- ***unknown map: …*** The map was deleted or is no longer shared with you.
- ***Image unavailable*** (in the editor): the photo was deleted, or you can no longer view its
  layer. Use **Change image** or **Remove image**.
- **Delete is grayed out:** delete protection is on for that story. A new story is not protected, and this release has no control in the portal that turns the protection on or off for a story: it can only have been set, and can only be cleared, through the API, by the owner or an administrator.
- **An administrator sees *Talus+ entitlement expired …*:** the term has ended. Stories keep
  working; new releases and support need renewal (**Upload renewal license…**).

## Related guides

- `guide-maps.md`
- `guide-layers.md`
- `guide-portal.md`
- `guide-dashboards.md`
- `guide-reports.md`
- `talus-plus-runbook.md`
