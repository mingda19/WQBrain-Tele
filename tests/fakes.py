"""Shared test doubles for BRAIN HTTP, the PTB job queue, and the bot context."""

AUTH_URL = "https://api.worldquantbrain.com/authentication"
PERSONA_URL = "https://api.worldquantbrain.com/authentication/persona?id=abc"


# ------------------------------------------------------------------ BRAIN HTTP


class FakeResponse:
    def __init__(self, status_code, headers=None, payload=None, url=AUTH_URL):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code // 100 != 2:
            raise RuntimeError(f"status {self.status_code}")


class FakeSession:
    """Stands in for ACE's SingleSession: scripted POSTs, expiry-shaped GETs.

    ``expiry=None`` models a dead session, which is what ACE's real
    ``check_session_timeout`` turns into 0.
    """

    def __init__(self, post_responses=(), expiry=14395.0):
        self._post_responses = list(post_responses)
        self._expiry = expiry
        self.auth = None
        self.posted = []
        self.cookies = type("Cookies", (), {"clear": lambda self_: None})()

    def set_expiry(self, expiry):
        self._expiry = expiry

    def post(self, url, **kwargs):
        self.posted.append(url)
        if not self._post_responses:
            return FakeResponse(201)
        nxt = self._post_responses.pop(0)
        return nxt() if callable(nxt) else nxt

    def get(self, url, **kwargs):
        if self._expiry is None:
            return FakeResponse(401, payload={})
        return FakeResponse(200, payload={"token": {"expiry": self._expiry}})


# --------------------------------------------------------------- PTB job queue


class FakeJob:
    def __init__(self, callback, when, name, chat_id=None):
        self.callback = callback
        self.when = when
        self.name = name
        self.chat_id = chat_id
        self.interval = None
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_once(self, callback, when, chat_id=None, name=None):
        job = FakeJob(callback, when, name, chat_id)
        self.jobs.append(job)
        return job

    def run_repeating(self, callback, interval, first=None, chat_id=None, name=None):
        job = FakeJob(callback, first, name, chat_id)
        job.interval = interval
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return tuple(j for j in self.jobs if j.name == name and not j.removed)

    live = get_jobs_by_name


# ------------------------------------------------------------------ bot context


class FakeBot:
    def __init__(self):
        self.messages = []
        self.actions = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})

    async def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))

    def texts(self):
        return [m["text"] for m in self.messages]


class FakeApplication:
    def __init__(self, config, brain=None):
        self.bot_data = {"config": config, "brain": brain, "warned": False}


class FakeContext:
    """Enough of PTB's CallbackContext for the handler logic under test."""

    def __init__(self, config, brain=None):
        self.job_queue = FakeJobQueue()
        self.application = FakeApplication(config, brain)
        self.bot = FakeBot()
        self.job = None
