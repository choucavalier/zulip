# Delete a topic

{!admin-only.md!}

We generally recommend against deleting topics, but there are a few
situations in which it can be useful:

* Clearing out test messages after setting up an organization.
* Clearing out messages from an overly enthusiastic bot.
* Managing abuse.

In most other cases, [renaming a topic](/help/rename-a-topic) is often a
better idea, or just leaving the topic as is. Deleting a topic can confuse
users who come to the topic later via an email notification.

Note that deleting a topic also deletes every message with that topic,
whereas [archiving a channel](/help/archive-a-channel) does not.

### Delete a topic

{start_tabs}

{tab|desktop-web}

{!topic-actions.md!}

1. Click **Delete topic**.

1. Approve by clicking **Confirm**.

{tab|mobile}

Access this feature by following the web app instructions in your
mobile device browser.

Implementation of this feature in the mobile app is tracked [on
GitHub](https://github.com/zulip/zulip-flutter/issues/1549). If
you're interested in this feature, please react to the issue's
description with 👍.

{end_tabs}

Note that deleting all of the individual messages within a particular
topic also deletes that topic. Structurally, topics are simply an
attribute of messages in Zulip.

## Related articles

* [Edit a message](/help/edit-a-message)
* [Delete a message](/help/delete-a-message)
* [Archive a channel](/help/archive-a-channel)
* [Message retention policy](/help/message-retention-policy)
