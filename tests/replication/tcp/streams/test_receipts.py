# -*- coding: utf-8 -*-
# Copyright 2019 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
from typing import Optional

from twisted.internet.interfaces import IConsumer, IPullProducer, IReactorTime
from twisted.internet.task import LoopingCall
from twisted.web.http import HTTPChannel

# from synapse.types import UserID
from synapse.handlers.typing import RoomMember
from synapse.http.site import SynapseRequest
from synapse.replication.http import streams
from synapse.replication.tcp.streams._base import ReceiptsStream
from synapse.util import Clock

from tests.replication.tcp.streams._base import BaseStreamTestCase
from tests.server import FakeTransport

logger = logging.getLogger(__name__)

USER_ID = "@feeling:blue"


class ReceiptsStreamTestCase(BaseStreamTestCase):
    servlets = [
        streams.register_servlets,
    ]

    def test_receipt(self):
        self.reconnect()

        # make the client subscribe to the receipts stream
        self.test_handler.streams.add("receipts")

        # tell the master to send a new receipt
        self.get_success(
            self.hs.get_datastore().insert_receipt(
                "!room:blue", "m.read", USER_ID, ["$event:blue"], {"a": 1}
            )
        )
        self.replicate()

        # there should be one RDATA command
        self.test_handler.on_rdata.assert_called_once()
        stream_name, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "receipts")
        self.assertEqual(1, len(rdata_rows))
        row = rdata_rows[0]  # type: ReceiptsStream.ReceiptsStreamRow
        self.assertEqual("!room:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event:blue", row.event_id)
        self.assertEqual({"a": 1}, row.data)

        # Now let's disconnect and insert some data.
        self.disconnect()

        self.test_handler.on_rdata.reset_mock()

        self.get_success(
            self.hs.get_datastore().insert_receipt(
                "!room2:blue", "m.read", USER_ID, ["$event2:foo"], {"a": 2}
            )
        )
        self.replicate()

        # Nothing should have happened as we are disconnected
        self.test_handler.on_rdata.assert_not_called()

        self.reconnect()
        self.pump(0.1)

        # We should now have caught up and get the missing data
        self.test_handler.on_rdata.assert_called_once()
        stream_name, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "receipts")
        self.assertEqual(token, 3)
        self.assertEqual(1, len(rdata_rows))

        row = rdata_rows[0]
        self.assertEqual("!room2:blue", row.room_id)
        self.assertEqual("m.read", row.receipt_type)
        self.assertEqual(USER_ID, row.user_id)
        self.assertEqual("$event2:foo", row.event_id)
        self.assertEqual({"a": 2}, row.data)

    def test_typing(self):
        typing = self.hs.get_typing_handler()

        room_id = "!bar:blue"

        self.reconnect()

        # make the client subscribe to the receipts stream
        self.test_handler.streams.add("typing")

        typing._push_update(member=RoomMember(room_id, USER_ID), typing=True)

        self.reactor.advance(0)

        # We should now see an attempt to connect to the master
        self._handle_replication_connection()

        self.test_handler.on_rdata.assert_called_once()
        stream_name, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "typing")
        self.assertEqual(1, len(rdata_rows))
        row = rdata_rows[0]
        self.assertEqual(room_id, row.room_id)
        self.assertEqual([USER_ID], row.user_ids)

        # Now let's disconnect and insert some data.
        self.disconnect()

        self.test_handler.on_rdata.reset_mock()

        typing._push_update(member=RoomMember(room_id, USER_ID), typing=False)

        self.test_handler.on_rdata.assert_not_called()

        self.reconnect()
        self.pump(0.1)

        # We should now see an attempt to connect to the master
        self._handle_replication_connection()

        self.test_handler.on_rdata.assert_called_once()
        stream_name, token, rdata_rows = self.test_handler.on_rdata.call_args[0]
        self.assertEqual(stream_name, "typing")
        self.assertEqual(1, len(rdata_rows))
        row = rdata_rows[0]
        self.assertEqual(room_id, row.room_id)
        self.assertEqual([], row.user_ids)

    def _handle_replication_connection(self):
        """
        """

        # We should have an outbound connection attempt.
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8765)

        # Set up client side protocol
        client_protocol = client_factory.buildProtocol(None)

        # Set up the server side protocol
        channel = _PushHTTPChannel(self.reactor)
        channel.requestFactory = SynapseRequest
        channel.site = self.site

        # Connect client to server nad vice versa.
        client_to_server_transport = FakeTransport(
            channel, self.reactor, client_protocol
        )
        client_protocol.makeConnection(client_to_server_transport)

        server_to_client_transport = FakeTransport(
            client_protocol, self.reactor, channel
        )
        channel.makeConnection(server_to_client_transport)

        # The request will now be processed by `self.site` and the response
        # streamed back.
        self.reactor.advance(0)

        # We tear down the connection so it doesn't get reused without our
        # knowledge.
        server_to_client_transport.loseConnection()
        client_to_server_transport.loseConnection()


def _log_request(request):
    """Implements Factory.log, which is expected by Request.finish"""
    # logger.info("Completed request %s", request)
    pass


class _PushHTTPChannel(HTTPChannel):
    """A HTTPChannel that wraps pull producers to push producers.

    This is a hack to get around the fact that HTTPChannel transparently wraps a
    pull producer (which is what Synapse uses to reply to requests) with
    `_PullToPush` to convert it to a push producer. Unfortunately `_PullToPush`
    uses the standard reactor rather than letting us use our test reactor, which
    makes it very hard to test.
    """

    def __init__(self, reactor: IReactorTime):
        super().__init__()
        self.reactor = reactor

        self._pull_to_push_producer = None

    def registerProducer(self, producer, streaming):
        # Convert pull producers to push producer.
        if not streaming:
            self._pull_to_push_producer = _PullToPushProducer(
                self.reactor, producer, self
            )
            producer = self._pull_to_push_producer

        super().registerProducer(producer, True)

    def unregisterProducer(self):
        if self._pull_to_push_producer:
            # We need to manually stop the _PullToPushProducer.
            self._pull_to_push_producer.stop()


class _PullToPushProducer:
    """A push producer that wraps a pull producer.
    """

    def __init__(
        self, reactor: IReactorTime, producer: IPullProducer, consumer: IConsumer
    ):
        self._clock = Clock(reactor)
        self._producer = producer
        self._consumer = consumer

        # While running we use a looping call with a zero delay to call
        # resumeProducing on given producer.
        self._looping_call = None  # type: Optional[LoopingCall]

        # We start writing next reactor tick.
        self._start_loop()

    def _start_loop(self):
        """Start the looping call to
        """

        if not self._looping_call:
            # Start a looping call which runs every tick.
            self._looping_call = self._clock.looping_call(self._run_once, 0)

    def stop(self):
        """Stops calling resumeProducing.
        """
        if self._looping_call:
            self._looping_call.stop()
            self._looping_call = None

    def pauseProducing(self):
        """Implements IPushProducer
        """
        self.stop()

    def resumeProducing(self):
        """Implements IPushProducer
        """
        self._start_loop()

    def stopProducing(self):
        """Implements IPushProducer
        """
        self.stop()
        self._producer.stopProducing()

    def _run_once(self):
        """Calls resumeProducing on producer once.
        """

        try:
            self._producer.resumeProducing()
        except Exception:
            logger.exception("Failed to call resumeProducing")
            try:
                self._consumer.unregisterProducer()
            except Exception:
                pass

            self.stopProducing()
