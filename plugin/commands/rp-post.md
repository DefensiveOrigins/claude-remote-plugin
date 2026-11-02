---
description: Leave a message on the remote server for its operator or another client
argument-hint: "[@host[:port]] [to:<client>] <message>"
---
Post a message with the `remote_post` tool from the remote-prompt MCP server.

Arguments: $ARGUMENTS

If the arguments start with `@host` or `@host:port`, pass that host (and port) to the tool. If a `to:<client>` token follows, pass that client name as `to`; otherwise the message goes to the server operator. Send the remaining text verbatim, then confirm the message id.
