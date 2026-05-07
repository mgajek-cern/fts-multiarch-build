# Transfer Scenarios Overview

| Source | Destination | Intermediary needed? |
|--------|-------------|----------------------|
| WebDAV | WebDAV | No — TPC native; StoRM-WebDAV ([TPC docs](https://italiangrid.github.io/storm/documentation/sysadmin-guide/1.11.20/installation-guides/webdav/tpc/index.html)), dCache ([WebDAV TPC docs](https://github.com/dCache/dcache/blob/master/docs/UserGuide/src/main/markdown/webdav.md)), and XrdHTTP ([xrootd-tpc](https://xrootd.web.cern.ch/doc/dev49/tpc_protocol.htm)) all support PULL-based HTTP COPY |
| S3 | WebDAV | Conditional — WebDAV pulls from S3 via pre-signed URL, but requires server-side support (dCache and StoRM-WebDAV do; not all WebDAV implementations do) |
| XrdHTTP | WebDAV | No — HTTP TPC (same mechanism as WebDAV→WebDAV; see [HTTP-TPC protocol spec](https://twiki.cern.ch/twiki/bin/view/LCG/HttpTpc)) |
| WebDAV | S3 | Yes — S3 cannot act as a TPC destination; requires FTS streaming or an S3-fronting gateway ([FTS3 S3 support](http://fts3-docs.web.cern.ch/fts3-docs/docs/s3_support.html)) |
| XrdHTTP | S3 | Yes — requires FTS streaming or a TPC-capable S3 gateway ([FTS3 S3 support](http://fts3-docs.web.cern.ch/fts3-docs/docs/s3_support.html)) |
| S3 | S3 | Yes — FTS streaming ([FTS3 S3 support](http://fts3-docs.web.cern.ch/fts3-docs/docs/s3_support.html)); note: native S3 replication tools (e.g. `aws s3 sync`) are outside the FTS/grid transfer scope |
| S3 | XrdHTTP | Conditional — no intermediary if the XrdHTTP server has S3 plugin or redirect support configured; FTS streaming otherwise |
| XrdHTTP | XrdHTTP | No — native HTTP TPC supported ([xrootd-tpc](https://xrootd.web.cern.ch/doc/dev49/tpc_protocol.htm)) |
| XRootD (`root://`) | XRootD (`root://`) | No — native XRootD TPC if both endpoints support it ([XRootD TPC in WLCG](https://www.epj-conferences.org/articles/epjconf/pdf/2020/21/epjconf_chep2020_04031.pdf)) |
| XRootD (`root://`) | WebDAV / S3 / other | Conditional — native TPC if the destination supports it; FTS fallback otherwise ([FTS3 docs](https://fts3-docs.web.cern.ch/fts3-docs/)) |

**NOTE:**
- **Third-Party Copy (TPC):** transfers data directly between storage endpoints, keeping FTS out of the data path. The *destination* storage initiates the transfer by pulling from the source. Not all storage systems support acting as a TPC destination. See the [HTTP-TPC protocol spec](https://twiki.cern.ch/twiki/bin/view/LCG/HttpTpc) and the [WLCG HTTP-TPC technical details](https://twiki.cern.ch/twiki/bin/view/LCG/HttpTpcTechnical).
- **Intermediary (streaming) transfers:** route data through the FTS server itself, which can introduce additional network load and bottlenecks when TPC is not an option. See [FTS3 documentation](https://fts3-docs.web.cern.ch/fts3-docs/).
- **"Conditional"** entries depend on server-side configuration or plugin support — check your specific deployment before assuming TPC is available.
