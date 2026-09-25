# STOXX certificate chain

`stoxx-ca-bundle.pem` contains the SSL.com chain needed to verify
`www.stoxx.com` on Linux while that server supplies a legacy cross-signed
intermediate. It is used only for STOXX requests and only after the normal
certifi bundle rejects the chain.

The certificates come from the issuer URLs published in the chain:

- `http://cert.ssl.com/SSL.com-TLS-I-RSA-R1.cer`
- `http://cert.ssl.com/SSLcom-TLS-Root-2022-RSA.cer`

SHA-256 fingerprints:

- Issuing RSA CA R1: `BF:BC:39:E9:FA:2B:84:C9:A9:23:37:E2:34:4E:E2:38:1D:8D:3D:3A:E8:EA:75:EF:4E:48:E5:80:7E:D8:0C:69`
- TLS RSA Root CA 2022: `8F:AF:7D:2E:2C:B4:70:9B:B8:E0:B3:36:66:BF:75:A5:DD:45:B5:DE:48:0F:8E:A8:D4:BF:E6:BE:BC:17:F2:ED`
