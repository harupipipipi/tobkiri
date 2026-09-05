# Rumi Connector Transport Gateway Pack

Provides one removable transport facade over the connector registry, verified
inbound broker, and receipt-gated outbound broker. Ingress and egress remain
separate permissions. Egress redeems a gateway receipt before issuing a second,
exact outbound-owner receipt.

The gateway owns no connector state, replay ledger, delivery ledger, network
server, vendor implementation, or credentials. Status exposes only public
configuration and a boolean indicating whether a credential reference exists.

