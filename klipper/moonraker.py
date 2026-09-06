"""Blocking Moonraker calls that do not stall Klipper's reactor.

Two extras need this: spool_guard asks for job metadata and Spoolman state,
chamber_preheat asks for the nozzle temperature history. Moonraker is on
loopback and answers in milliseconds, but a stalled socket inside a reactor
callback freezes the MCU link along with everything else, so the request runs
on a thread while the caller yields with reactor.pause().
"""

import json
import threading
import urllib.error
import urllib.parse
import urllib.request


class MoonrakerError(Exception):
    """A request failed. Callers decide whether that is fatal.

    `status` carries the HTTP status when there was one, so a caller can tell
    "this endpoint does not exist here" from "the host did not answer". Those
    deserve opposite responses: a 404 on the Spoolman proxy means Spoolman is
    simply not configured, which is most users, and refusing their prints over
    it would be absurd. A timeout means something that should be there is not.
    """

    def __init__(self, message, status=None, key=None):
        super().__init__(message)
        self.status = status
        # Which of the batched calls failed. Matching on the message text would
        # work today and break the first time somebody rewords a key.
        self.key = key


class MoonrakerClient:
    def __init__(self, reactor, base_url, timeout):
        self.reactor = reactor
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _blocking(self, path, body=None):
        url = self.base_url + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response).get("result")

    def run(self, calls):
        """Run `calls`, a list of (key, path, body), off the reactor thread.

        Returns a dict of results. Raises MoonrakerError on the first failure,
        naming the key that failed so the caller can say which lookup broke.
        """
        box = {}

        def worker():
            for key, path, body in calls:
                try:
                    box[key] = self._blocking(path, body)
                except Exception as error:
                    box["__failed__"] = (key, error)
                    return

        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()
        limit = self.timeout * len(calls) + 1.0
        deadline = self.reactor.monotonic() + limit
        eventtime = self.reactor.monotonic()
        while thread.is_alive() and eventtime < deadline:
            eventtime = self.reactor.pause(eventtime + 0.1)
        if thread.is_alive():
            raise MoonrakerError(
                "Moonraker did not answer within %.0fs" % limit
            )
        if "__failed__" in box:
            key, error = box["__failed__"]
            # urllib raises HTTPError for a status and URLError for transport,
            # and only the former carries `code`.
            raise MoonrakerError("%s (%s)" % (key, error),
                                 status=getattr(error, "code", None), key=key)
        return box

    @staticmethod
    def quote(value):
        return urllib.parse.quote(value)
