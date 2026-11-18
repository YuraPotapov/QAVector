"""Microsoft Teams: posts to a channel or chat through a Workflows webhook (QCR ADR-015).

A connection is one channel or chat; its webhook URL is a secret, kept as a
Jira token is. ``webhook`` builds the card and sends it; ``history`` keeps what
was posted (the content, never the URL); ``capabilities`` is the provider the
reader and QCR use.
"""
