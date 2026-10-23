FROM docker.io/library/postgres@sha256:f97ca6038b7641359fef23126949d824ac68edd8a7081b424d6a78e1f7076f8e
# Inputs are downloaded and hash-checked by the owned fixture manager. No live APT resolver.
COPY debs/ /locked-debs/
COPY input-sha256.txt /locked-input-sha256.txt
COPY expected-packages.tsv /locked-expected-packages.tsv
RUN set -eu; \
    cd /locked-debs; \
    sha256sum -c /locked-input-sha256.txt; \
    DEBIAN_FRONTEND=noninteractive dpkg --unpack ./*.deb; \
    DEBIAN_FRONTEND=noninteractive dpkg --configure -a; \
    test -z "$(dpkg --audit)"; \
    dpkg-query -W -f '${Package}\t${Version}\t${Architecture}\n' | LC_ALL=C sort > /locked-actual-packages.tsv; \
    cmp /locked-expected-packages.tsv /locked-actual-packages.tsv
