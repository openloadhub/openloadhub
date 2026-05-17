# JMeter Third-Party Dependency Manifest

The public demo image downloads Apache JMeter from the official Apache archive during Docker build. The repository does not commit the JMeter distribution or plugin binaries.

## Apache JMeter

| Field | Value |
| --- | --- |
| Name | Apache JMeter |
| Version | 5.4.3 |
| Source URL | `https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-5.4.3.tgz` |
| SHA512 | `e88802cc0dfcd6a2c8554911ae4574d7cfafcc8c6be6ade810b4677b7351831b0680d81cf2b0fb5bb4b9b3cf437528a044d7da74214a1bee351b273dbb53e439` |
| License | Apache-2.0 |
| Target | `/opt/apache-jmeter-5.4.3` |
| Proof | Docker build verifies the SHA512 before extracting. |

## gRPC Request Plugin

| Field | Value |
| --- | --- |
| Name | `jmeter-grpc-request` |
| Version | 1.2.6 |
| Source URL | `https://github.com/zalopay-oss/jmeter-grpc-request/releases/download/v1.2.6/jmeter-grpc-request.jar` |
| SHA256 | `3bdd71cfb13634d29379e9d73f21912330f0416e02386e853f513ece679aafae` |
| License | Apache-2.0 |
| Target | `/opt/apache-jmeter-5.4.3/lib/ext/jmeter-grpc-request-v2.jar` |
| Proof | Docker build verifies SHA256, then strips bundled SLF4J binding classes to avoid conflicts with JMeter logging. |
