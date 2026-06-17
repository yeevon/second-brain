from datetime import UTC, datetime
from types import SimpleNamespace


class FakeDiscordAuthor:
    def __init__(self, author_id=300, *, bot=False):
        self.id = author_id
        self.bot = bot


class FakeDiscordGuild:
    def __init__(self, guild_id=100):
        self.id = guild_id


class FakeDiscordAttachment:
    def __init__(
        self,
        *,
        filename="screenshot.png",
        content_type="image/png",
        size=42,
        url="https://cdn.discordapp.com/attachments/screenshot.png",
    ):
        self.filename = filename
        self.content_type = content_type
        self.size = size
        self.url = url


class FakeReceiptMessage:
    def __init__(self, message_id, content, channel):
        self.id = message_id
        self.content = content
        self.channel = channel
        self.edit_attempts = 0

    async def edit(self, *, content):
        self.edit_attempts += 1
        self.channel.edit_attempts += 1
        if self.channel.fail_receipt_edit:
            raise RuntimeError("simulated receipt edit failure")
        self.content = content
        self.channel.edited_receipts.append((self.id, content))


class FakeDiscordMessage:
    def __init__(
        self,
        message_id=1001,
        *,
        content="Review reconnect handling.",
        author=None,
        guild=None,
        channel=None,
        channel_id=200,
        webhook_id=None,
        attachments=None,
        reference=None,
        created_at=None,
    ):
        self.id = message_id
        self.content = content
        self.author = author or FakeDiscordAuthor()
        self.guild = guild if guild is not None else FakeDiscordGuild()
        self.channel = channel or FakeDiscordChannel(channel_id=channel_id)
        self.webhook_id = webhook_id
        self.attachments = attachments or []
        self.reference = reference
        self.created_at = created_at or datetime(2026, 6, 7, 12, 0, tzinfo=UTC)


class FakeDiscordChannel:
    def __init__(self, history_messages=None, *, channel_id=200):
        self.id = channel_id
        self.history_messages = history_messages or []
        self.messages = {}
        self.sent_receipts = []
        self.edited_receipts = []
        self.replacement_receipts = []
        self.edit_attempts = 0
        self.next_receipt_id = 9001
        self.fail_initial_send = False
        self.fail_receipt_edit = False
        for message in self.history_messages:
            message.channel = self

    async def send(self, content):
        if self.fail_initial_send:
            raise RuntimeError("simulated initial receipt failure")
        receipt_id = self.next_receipt_id
        self.next_receipt_id += 1
        receipt = FakeReceiptMessage(receipt_id, content, self)
        self.messages[receipt_id] = receipt
        self.sent_receipts.append((receipt_id, content))
        if len(self.sent_receipts) > 1:
            self.replacement_receipts.append((receipt_id, content))
        return SimpleNamespace(id=receipt_id)

    async def fetch_message(self, message_id):
        return self.messages[int(message_id)]

    def history(self, *, limit, after, oldest_first):
        after_id = 0 if after is None else after.id
        messages = [message for message in self.history_messages if message.id > after_id]
        if oldest_first:
            messages = sorted(messages, key=lambda message: message.id)
        return FakeHistory(messages[:limit])


class FakeDiscordClient:
    def __init__(self, channel=None):
        self.channel = channel or FakeDiscordChannel()

    def get_channel(self, channel_id):
        if self.channel.id == channel_id:
            return self.channel
        return None

    async def fetch_channel(self, channel_id):
        if self.channel.id == channel_id:
            return self.channel
        raise KeyError(f"channel not found: {channel_id}")

    @property
    def sent_receipts(self):
        return self.channel.sent_receipts

    @property
    def edited_receipts(self):
        return self.channel.edited_receipts

    @property
    def replacement_receipts(self):
        return self.channel.replacement_receipts


class FakeHistory:
    def __init__(self, messages):
        self.messages = messages

    def __aiter__(self):
        self._iterator = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc
