# Rumi Connector Inbound Broker Pack

Selects one vendor adapter by exact manifest key, accepts only verified normalized
events, rejects replay payload changes, and invokes selected route projections.
It stores body hashes and redacted event metadata, never raw credentials or
secret-bearing reply material, and imports neither chat nor Company.

