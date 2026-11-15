# Pulling tables from your SQL database

Talus can keep a **read-only copy** of a table or view from a SQL database your organization already runs, such as a
work-order, permitting, or billing system, and refresh that copy on a schedule. The copy behaves like any other Talus table
or point layer: it can go on maps, be related to other layers, and be shared. This guide covers connecting to a database,
creating a pull, and reading what a refresh did. It is part of Talus Community and Talus+.

**Talus never writes to your database.** It sends only queries it builds itself, and it reads only the table or view you chose. On PostgreSQL, MySQL and MariaDB the session is also set read-only, so the database refuses a write. On SQL Server the connection only declares read-only intent, which a single server does not enforce, so give Talus a login that can only read.

## Who can do what

| Who | Can |
|---|---|
| Publisher | Add their own database connections, create pulls, set schedules, refresh, detach |
| Administrator | Everything a publisher can, plus an overview of every connection and pull in the organization, and the ability to remove a connection or detach a pull |
| Editor, viewer | Use the resulting table or layer if it is shared with them. They see the message *"Pulling data from a database creates tables, so this is for publishers. Ask an admin for the publisher role."* |

Connections and pulls belong to the person who made them. Sharing the **copy** works like sharing any layer and never
exposes the database login.

## Before you start

1. **The Talus server must be able to reach the database server** over your network.
2. **A read-only login.** Ask your database administrator for a login that can only read, ideally on a view made for
   Talus. Talus stores the password encrypted and never shows it again. The login's permissions are the limit of what that
   stored credential could ever reach, so do not use a login that can write.
3. **The server's host name or address, the port, and the database name.**
4. **How the connection is encrypted.** If your server presents a certificate from your organization's own certificate
   authority, have that CA bundle (PEM text) ready to paste.
5. **The time zone of the server's date-and-time values**, for timestamp columns that carry no zone.

## What is supported

| Supported | Not supported |
|---|---|
| PostgreSQL and PostGIS | Oracle (listed, not available) |
| Microsoft SQL Server with a SQL login | Windows authentication to SQL Server |
| MySQL and MariaDB | Reading a geometry column directly |
| Tables and views in any schema | Lines and polygons |
| Points from two coordinate columns | More than 200 columns in one pull |

There is deliberately **no filter box and no free-text SQL**. If you want a subset of rows, have your database
administrator create a view; the view is the filter. To place points from a spatial table, expose the coordinates as two
columns in a view. Binary and geometry columns are skipped, and the column list says so.

## Add a connection

1. Open the **Catalog** and choose the **Databases** tab, or open **New** in the top bar and choose **Database pull**. Both open the same page.
2. Choose **+ Add connection**.
3. Fill in **Name**, **Database type**, **Server (host name or IP address)**, **Port**, **Database**, **User name**, and
   **Password**.
4. Under **Encryption (TLS)**, pick one:
   - **Encrypted, certificate verified (recommended)**. Paste your CA bundle if the server's certificate comes from your
     own certificate authority.
   - **Encrypted, host name not checked (PostgreSQL: certificate not verified; SQL Server: chain verified against the bundle)**. The traffic is encrypted, but with nothing pasted Talus does not confirm which
     server answered. Treat it as a setting for a trusted local network.
   - **Not encrypted (LAN only, last resort)**. Everything, including the password, crosses the network in clear.
5. Enter the **Time zone of the server's date-and-time values**, for example `America/Chicago`.
6. Choose **Save**.
7. Choose **Test** on the connection's row. **Last test** shows **OK**, or **failed** with a plain reason. The password is
   never shown in a failure message.

To change a connection, choose **Edit**. A saved password shows as **Password set**; choose **Replace** to enter a new one
or **Clear** to remove it.

## Create a pull

1. On a tested connection, choose **New pull**.
2. *Which table or view holds the rows you want in Talus?* Type part of a name in **Find** to narrow the list. The list
   shows what identifies a row in each table. Choose **Choose** beside the one you want.
3. Tick the columns to bring across. The list shows each column's **Source type** and what it becomes **In Talus**. For a
   date-and-time column you can tick **stored in UTC** or **keep the date only**.
4. *Where do these rows go on the map?* Pick one:
   - **Nowhere — a table**. Join or relate it to a layer later.
   - **Points from two coordinate columns**. Choose the **X or longitude column** and the **Y or latitude column**, and
     enter the **Coordinate system (EPSG code)**, such as `4326` for longitude and latitude.
   - **Addresses — geocode the table after the pull**. The pull creates a plain table. Turning its addresses into
     points is a separate step: when the pull finishes, open **Geocode addresses** in the Catalog and choose the table
     under **A table or layer already in Talus**. The point layer it makes is a copy made at that moment. It does not
     refresh with the pull; geocode the table again when you want a fresh copy. See `guide-geocoding.md`.
5. *Which column identifies a row?* Pick the key column so later refreshes can tell which rows changed. If you pick
   **none**, the whole copy is replaced on each refresh.
6. *How often should Talus refresh it?* Pick **Nightly at** an hour, **Every** so many hours, or **Only when I press
   Refresh now**. Times are in the connection's time zone.
7. Enter the **Name of the new Talus table**.
8. Choose **Create and pull now**. The first pull copies every row. When it finishes, choose **Open it**.

## Read what a refresh did

The **Pulls** list shows each pull's source, column count, schedule, and **Last run**.

- A finished run reads, for example, *"12 added, 3 changed, 1 removed, 410 unchanged"*. A run that found nothing new
  reads *"nothing to copy"*. Rows the copy could not accept are counted as *"refused"*.
- Only rows that changed are written. A changed row keeps its Talus identity, so relates and selections survive a refresh.
- While a run is in progress the row reads *"queued"* or *"running"* with a row count, and offers **Cancel**. A canceled
  run reads *"canceled — last good copy kept"*.
- If a run fails, **the last good copy stays**. The pull shows a **stale since** badge with the reason, and the schedule
  tries again at its next slot.
- Choose **Refresh now** to run a pull immediately, or **Change** to edit its schedule.

## The copy is read-only until you detach it

While a pull is attached, its copy in Talus cannot be edited, so it always matches the source. Choose **Detach** to end the
refresh and turn the copy into an ordinary table you can edit. The source is untouched. To pull from the source again,
create a new pull.

Removing a connection with **Remove** also removes its pulls. The Talus tables already copied stay, and become editable
again.

Two events change a pull without anyone pressing a button:

- **The owner's account is disabled, or set to a role below publisher.** The pull switches to manual and its schedule reads
  *"manual — owner disabled"* or *"manual — owner role changed"*, with the date. It stops refreshing until its owner sets a
  schedule again.
- **The copy is transferred to another owner.** It is detached at that moment, and the new owner receives an ordinary,
  editable table.

## How types come across

Dates stay dates. Date-and-time values become text in ISO 8601 form with an offset; a value with no zone takes the
connection's time zone unless you marked the column as stored in UTC. The data table and pop-ups show that text as a date and time in the viewer's own time zone; exports carry it as stored (`guide-layers.md`). Whole numbers and decimals keep their values. Very
large integers become decimal numbers.

## Limits, and what it deliberately does not do

- It does not write to the source database, ever.
- It is not an extract-transform-load tool. A column list, a type mapping, and an optional coordinate pair are the whole
  shape of a pull.
- It does not follow a server's request to redirect the connection to a different host. Point the connection at that host
  directly.
- A database that is part of the Talus deployment itself cannot be used as a source.
- A source query that runs past ten minutes is stopped.
- A database with more than 2,000 tables and views is too large to list. Ask your database administrator for a login
  limited to a schema of views made for Talus.

## If something goes wrong

| What you see | What it means, and what to do |
|---|---|
| *could not reach … (refused, timed out, or the port is wrong)* | The Talus server cannot open the connection. Check the host, the port, and any firewall between the two servers. |
| *the database refused the user name or password* | Re-enter the login. Choose **Edit**, then **Replace** beside the password. |
| *that database does not exist on the server* | Check the **Database** name, and that the login may open it. |
| *the TLS handshake failed — the server certificate is not trusted by this TLS mode* | Paste the server's or your certificate authority's certificate as the CA bundle, or choose a different encryption mode. |
| *the login lacks permission on that table or schema* | Ask your database administrator to grant SELECT on the table or view. |
| *that table or view is not in the database catalog (renamed or dropped?)* | The source changed. Create a new pull against the current name. |
| *a column in this pull no longer exists in the source (renamed or dropped?)* | The source changed. The last good copy stays. Create a new pull with the current columns. |
| *the source query ran past the time limit* | The table is too slow to read in one pass. Ask for a view that returns fewer rows or columns. |
| *the key column must be a primary key or a single-column unique index in the source* | Pick a different key column, or pick **none**. |
| *these coordinate columns do not name a lon/lat pair — declare the EPSG code* | Enter the coordinate system's EPSG code. |
| *a connection with that name already exists* | Use a different **Name**. |
| A password field showing that the stored password cannot be opened | The server's encryption key changed. Re-enter the password. |

## Related guides

- `guide-layers.md` — tables, relates, and fields
- `guide-geocoding.md` — turning an address column into points
- `guide-maps.md` — putting the copy on a map
- `guide-portal.md` — sharing the copy
- `guide-administration.md` — the administrator's overview of every connection and pull
- `security-overview.md` — the outbound connections a deployment can make
