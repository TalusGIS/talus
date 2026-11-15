# The Talus Helper (Talus+)

The Talus Helper is an assistant built into a Talus+ portal. It answers questions from the Talus documentation and, for
administrators, can read a snapshot of the deployment's own health to help diagnose a problem. It is informational: it
explains, cites its sources, and suggests steps for you to take. **It never runs anything, changes any setting, or edits any
data.** This guide covers asking a question, sending diagnostics, the two Administration pages for AI, and what to do when
the Helper is paused.

## Who can do what

| Who | Can |
|---|---|
| Any signed-in user | Open the Helper and ask questions answered from the documentation |
| Administrator | The same, plus include diagnostics from the deployment, and use the **AI** and **AI usage** pages in the **Talus+** group of the **Admin** tab |

The **Helper** button appears in the top bar only when the deployment has Talus+ with the AI assists turned on. On Talus
Community, on an air-gapped deployment, or before an administrator has activated AI, there is no button and no AI at all.

## What the Helper can and cannot answer

The Helper answers **only from the Talus documentation published with your release**: the guides for using the portal, the
guides for the Talus+ modules, and the runbooks for installing, upgrading, backing up, and securing the server. It does not
search the internet, and it does not look at your layers, maps, or records.

When the documentation does not cover your question, the Helper says so instead of guessing. You will see *"The
documentation does not cover this. Use the summary below to contact support."* That is the Helper working as designed, not
a failure. The summary it gives you is written to be sent to Talus support.

**Each question stands alone.** The Helper does not remember your previous question, so put the whole question in each
time. Questions that name the thing you are working on get better answers:

- *"How do I add a chart to a dashboard?"* rather than *"dashboards"*
- *"How do I share a layer with a group?"* rather than *"sharing"*
- *"What does the stale badge on a database pull mean?"* rather than *"why is it broken"*

## Ask a question

1. Choose **Helper** in the top bar. The **Talus Helper** panel opens.
2. Type your question. A question can be up to 4,000 characters.
3. Choose **Ask**, or press Ctrl+Enter (Command+Enter on a Mac).

The answer has up to five parts:

- **The answer**, in plain text.
- **Sources**: the document and section each part of the answer came from.
- **Next steps**: a checklist of steps for you to perform. Tick them off as you go. The Helper does not perform them.
- **For support**: a short summary you can send to Talus support, shown when the documentation did not resolve the
  question. Choose **Copy for support** to copy it with the request ID.
- **A provenance line** at the bottom naming the AI model, the prompt version, the documentation version, and the request
  ID. Every Helper answer is labeled as AI-generated.

## Send diagnostics with a question (administrators)

Use this when something in the deployment is not working and you want the Helper to look at the deployment's own state.

1. Open the Helper and tick **Include diagnostics from this deployment (administrators)**.
2. Read the preview under **This is what will be sent to Talus AI.** It lists each section with a short description and a
   count, the total size, and a fingerprint of the content.
3. Choose **show what will be sent** beside any section to read exactly what it contains.
4. Untick any section you do not want to send. At least one section must stay ticked.
5. Type what is wrong, for example *"Why is sharing not working?"* If a Talus error message gave you an error code, paste
   the code into your question. The preview then adds the records for that one request.
6. Choose **Send and ask**.

Nothing leaves your server until you choose **Send and ask**. Secrets are removed before the preview is shown. Names and
host names are kept, because they are what makes a diagnosis useful; untick a section to leave it out.

The sections you can send:

| Section | What it contains |
|---|---|
| **Service checks** | Whether the database, sign-in, permissions, storage, tiles, the portal certificate, the clock, and Talus AI are up, down, or slow right now |
| **Operational state** | What is down and what issues are open, the newest backup, maintenance runs, storage, and the installed version |
| **Recent warnings and errors** | The last 200 warning and error records from the portal |
| **The error code in your question** | The records of one request, once you paste its error code |
| **Failed data pulls and imports** | The last 10 failed database pulls and the last 10 failed file imports, with the reason each recorded |
| **Other services' errors** | The last 50 error and warning lines from the sign-in service, the database, and the web edge |
| **Recent admin actions** | The last 50 administrative actions: who changed what |
| **Environment** | The environment name, sign-in mode, module names, and version |
| **License status** | Whether the license is valid, its modules, and its term |

A diagnosis ends with the steps for you to perform, taken from the runbooks, and a support summary that carries the request
ID. The Helper sees what the portal records about itself. It does not see the host operating system, so some problems
still end with a summary to send to support.

## What leaves your server, and what is kept

Your portal talks to a service that Talus GIS LLC operates, called Talus AI, which talks to the model provider under
Talus's own account. Your deployment never holds a model provider's key.

- **What leaves:** your question, and in diagnose mode the sections you approved.
- **What never leaves:** your GIS data. The person asking is known to Talus AI only by a pseudonym; names are
  resolved on your own server. Diagnostics are the exception: user names and host names that appear in the
  sections you send go as they are (see above), and you can untick a section to leave it out.
- **What is kept:** usage records only: counts, times, request IDs, the model and versions used and, when diagnostics were sent, the names of the sections and a fingerprint of their content. The text of questions, answers, and
  diagnostics is not stored by your portal or by Talus AI.

The security overview and the data-processing terms of your agreement describe this in full.

## The AI page (administrators)

On the **Admin** tab, under the **Talus+** group, the **AI** page shows the state of the AI assists for the whole deployment.

- **Status** reads **Connected**, **Paused**, **Gateway unreachable**, **Not activated**, **Not available in this
  environment**, or **Credential invalid**, **Credential revoked** or **Credential expired**.
- **Licensed to**, **Term ends**, and **Credential** show whose credential is installed, when the term ends, and the last
  four characters of the stored credential.

To turn the AI assists on, upload your activation file on the **License** page in the same **Talus+** group, which installs
the license and activates AI in one step. To do it by hand, paste the activation code from that file into the box on the AI page and choose
**Activate**. An activation code works once.

To turn the AI assists off, choose **Remove AI credential…** and then **Remove**. The Helper is switched off for everyone.
Nothing else in the portal changes.

## The AI usage page (administrators)

The **AI usage** page, also in the **Talus+** group, shows how much of the allowance included with Talus+ has been used.

- The large figure is the percentage **of the included allowance used this term**, with a marker showing how much of the
  term has passed. Below it the page reads **on pace for the term**, **ahead of the term's pace**, or **behind the term's
  pace**.
- **Last 30 days**, **Last 24 hours**, and **Today (UTC)** count questions asked. Today's figure is shown against the
  daily cap, which resets at midnight UTC.
- **Who is asking**, **By operation**, and **Recent activity** break the history down. Names on this page are resolved on
  your own server.

There is no bill behind these numbers. When the allowance is used up, the Helper pauses and Talus contacts you. More
allowance is a quote you choose to accept, never an automatic charge.

## Limits, and what the Helper deliberately does not do

- It does not act. It cannot restart a service, change a setting, share a layer, or edit a record.
- It does not read your GIS data, and it cannot answer questions about what is in your layers.
- It does not hold a conversation. Each question is answered on its own.
- It does not answer from general knowledge. If the documentation does not say it, the Helper does not say it.
- It is not a person. Talus+ support is reached by email, and the support summary is written for that.
- It needs an outbound connection from your server to Talus AI. An air-gapped deployment has no Helper and keeps every
  other Talus+ module.

## If something goes wrong

When the Helper cannot answer, it shows a notice ending *"The rest of Talus is unaffected."* The notices and what to do:

| What you see | What it means, and what to do |
|---|---|
| *The documentation does not cover this. Use the summary below to contact support.* | The answer is not in the documentation. Send the summary to Talus support. |
| *One moment — too many questions at once.* | A short limit on how fast questions can be asked. Wait a moment and ask again. |
| *Today's AI allowance is used up. It resets at midnight UTC.* | The daily cap was reached. Ask again tomorrow. |
| *The included AI allowance for this term is used up. AI pauses here — it never bills. Talus will be in touch.* | The term allowance was reached. An administrator can see the figures on the AI usage page. |
| *Talus AI could not be reached from this deployment.* | Outbound HTTPS from your server to Talus AI is blocked or down. Ask whoever manages your firewall. |
| *The AI model is temporarily unavailable.* or *Talus AI is pausing all customers briefly. Try again later.* | A temporary condition on the Talus side. Try again later. |
| *No Talus AI token has been added yet.* | AI has not been activated. An administrator uploads the activation file. |
| *The Talus AI token has expired. Renew with Talus to resume.* | The term ended. Everything else in Talus+ keeps working. |
| *The stored AI token cannot be read (encryption key changed?). An administrator should re-paste it.* | The server's encryption key changed. An administrator activates AI again. |
| *Only administrators can send diagnostics.* | Diagnostics are for administrators. Ask your question without them, or ask an administrator. |
| *Diagnostics are not included in this deployment's Talus AI plan.* | Diagnose mode is not part of this deployment's plan. Questions from the documentation still work. |
| *Choose at least one diagnostics section, or switch diagnostics off.* | Tick a section, or untick **Include diagnostics**. |

## Related guides

- `guide-portal.md`, `guide-layers.md`, `guide-maps.md` — using the portal
- `guide-dashboards.md`, `guide-reports.md`, `guide-story-maps.md` — the Talus+ modules
- `guide-administration.md` — the Administration pages
- `talus-plus-runbook.md` — installing Talus+, activating and removing AI, and running air-gapped
- `security-overview.md` — how the AI module handles data
- `faq.md` — what the Helper sees, in question-and-answer form
