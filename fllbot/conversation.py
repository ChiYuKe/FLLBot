"""Shared conversation identity for QQ entry points. Account zero is legacy scope."""
import asyncio
import json
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationKey:
    platform: str
    self_id: int
    scene: str
    scene_id: int


@dataclass(frozen=True)
class ReceiveSnapshot:
    data_epoch: int
    received_at: float
    deadline: float
    monotonic_deadline: float
    queue_deadline: float
    account_epoch: int = 0
    turn_id: str = ''
    admission: object | None = None
    scope_generation: int = 0
    memory_allowed: bool | None = None

    @classmethod
    def capture(cls, generation: int, options, account_epoch: int = 0, turn_id: str = '', scope_generation: int = 0):
        now, monotonic = time.time(), time.monotonic()
        return cls(generation, now, now + getattr(options, "event_deadline_seconds", 120.0),
                   monotonic + getattr(options, "event_deadline_seconds", 120.0),
                   monotonic + getattr(options, "queue_timeout_seconds", 30.0), account_epoch, turn_id, scope_generation=scope_generation)


_received: ContextVar[ReceiveSnapshot | None] = ContextVar('received', default=None)


def current_receive() -> ReceiveSnapshot | None:
    return _received.get()


@contextmanager
def receive_scope(snapshot: ReceiveSnapshot):
    token = _received.set(snapshot)
    try:
        yield
    finally:
        _received.reset(token)


@dataclass(frozen=True)
class MessageEnvelope:
    event_key: str
    turn_id: str
    self_id: int
    scene: str
    scene_id: int
    sender_id: int
    message_id: int
    received_at: float
    segments_json: str
    quote_id: str
    stable_id: bool
    platform: str = 'qq'

    @classmethod
    def from_event(cls, event, connection):
        self_id = int(event.self_id or connection.self_id or 0)
        identity = f'qq:{self_id}:{event.scene}:{event.scene_id}:{event.message_id}'
        stable = bool(event.message_id)
        turn_id = uuid.uuid5(uuid.NAMESPACE_URL,identity).hex if stable else uuid.uuid4().hex
        segments = [segment.to_dict() for segment in event.message]
        quote_id = next((str(segment.get('id') or '') for segment in event.message if segment.type=='reply'),'')
        return cls(identity if stable else 'ephemeral:'+turn_id,turn_id,self_id,event.scene,event.scene_id,
                   event.user_id,event.message_id,time.time(),json.dumps(segments,ensure_ascii=False),quote_id,stable)


class CapacityExceeded(ValueError):
    pass


class ServiceClosing(CapacityExceeded):
    pass


class AdmissionTicket:
    def __init__(self, gate, key):
        self.gate, self.key, self.released = gate, key, False

    def release(self):
        if self.released:
            return
        self.released = True
        self.gate.used -= 1
        remaining = self.gate.counts[self.key] - 1
        if remaining:
            self.gate.counts[self.key] = remaining
        else:
            del self.gate.counts[self.key]


class ConversationCapacity:
    def __init__(self, configuration):
        self.configuration = configuration
        self.accepting = True
        self.used = 0
        self.counts: dict[ConversationKey, int] = {}

    def open(self) -> None:
        if self.used:
            raise RuntimeError('Cannot reopen capacity with active admissions')
        self.accepting = True

    def close(self) -> None:
        self.accepting = False

    def ensure_open(self) -> None:
        if not self.accepting:
            raise ServiceClosing('service_stopping')

    def status(self) -> dict:
        options = self.configuration()
        return {'accepting':self.accepting,'used':self.used,'capacity':options.max_pending_events+max(1,options.max_concurrency),
                'conversation_capacity':options.max_pending_per_conversation+1,'active_keys':len(self.counts)}

    def reserve(self, key: ConversationKey) -> AdmissionTicket:
        self.ensure_open()
        options = self.configuration()
        # Capacity includes configured pending slots and the fixed active worker slots.
        if self.used >= options.max_pending_events + max(1,options.max_concurrency):
            raise CapacityExceeded('global_queue_full')
        if self.counts.get(key,0) >= options.max_pending_per_conversation + 1:
            raise CapacityExceeded('conversation_queue_full')
        self.used += 1
        self.counts[key] = self.counts.get(key,0) + 1
        return AdmissionTicket(self,key)

    @contextmanager
    def admit(self, ctx):
        self.ensure_open()
        key = ConversationKey('qq',ctx.self_id,ctx.event.scene,ctx.event.scene_id)
        inherited = ctx.received.admission
        if inherited is not None:
            if inherited.released or inherited.key != key:
                raise CapacityExceeded('admission_invalid')
            yield inherited
            return
        ticket = self.reserve(key)
        try:
            yield ticket
        finally:
            ticket.release()


class ConversationLockPool:
    def __init__(self):
        self.slots = {}

    def lease(self, key):
        return ConversationLockLease(self,key)


class ConversationLockLease:
    def __init__(self, pool, key):
        self.pool, self.key, self.slot = pool, key, None

    async def acquire(self):
        slot = self.pool.slots.setdefault(self.key, [asyncio.Lock(),0])
        slot[1] += 1
        try:
            await slot[0].acquire()
        except BaseException:
            slot[1] -= 1
            if not slot[1]:
                self.pool.slots.pop(self.key,None)
            raise
        self.slot = slot
        return True

    def release(self):
        if self.slot is None:
            raise RuntimeError('conversation lock is not acquired')
        slot = self.slot
        slot[0].release()
        slot[1] -= 1
        if not slot[1]:
            self.pool.slots.pop(self.key,None)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        self.release()


_turn_scope: ContextVar[str] = ContextVar('business_turn_id',default='')


def current_turn_id() -> str:
    return _turn_scope.get()


@contextmanager
def turn_scope(turn_id: str):
    token = _turn_scope.set(turn_id)
    try:
        yield
    finally:
        _turn_scope.reset(token)
