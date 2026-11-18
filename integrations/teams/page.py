"""What the Integrations page asks of a Teams connection (QCR ADR-015): reads, the
person's own post - no approval, the person is the one sending -, Send again (a
new post with an old one's words), and a test post."""

#: A test post's words: it says what it is, so nobody acts on it.
TEST_POST = {"title": "QAVector test", "text": "A test post from QAVector: this channel's connection works."}


def call(connection, operation, request):
    """One Teams operation from the page (ADR-015): reads, the person's own post
    (no approval - the person is the one sending), Send again, a test post."""
    from integrations.contracts import WriteError
    from integrations.teams import capabilities, history
    from integrations.teams.webhook import SendError
    page = {"label": "This page"}
    try:
        if operation in ("preview", "history", "post_of"):
            result = capabilities.read(operation, connection, connection["token"], request)
        elif operation == "post":
            result = capabilities.post(connection, request, page)
        elif operation == "again":
            # Send again: a new post with the old one's words - it asked first (D3).
            old = history.get(connection["log"], str(request.get("id") or ""))
            if old is None or old.get("connection") != connection["id"]:
                return {"ok": False, "kind": "not_found", "error": "There is no such post."}
            result = capabilities.post(connection, {"title": old.get("title") or "", "text": old.get("text") or "",
                                                    "mentions": old.get("mentions") or []},
                                       page, again_of=old["id"])
        elif operation == "test":
            result = capabilities.post(connection, TEST_POST, {"label": "Test from this page"})
        else:
            return {"ok": False, "kind": "invalid", "error": "Teams has no operation %r." % operation}
    except WriteError as exc:
        return dict(exc.details, ok=False, kind=exc.code, error=str(exc))
    except SendError as exc:
        return {"ok": False, "kind": "network" if not exc.status else "rejected", "status": exc.status,
                "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "kind": "invalid", "error": str(exc)}
    return {"ok": True, "connection": connection["id"], "operation": operation, "result": result}
