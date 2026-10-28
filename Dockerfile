FROM golang:1.27.3-bookworm AS build
WORKDIR /src
COPY go.mod *.go trailmix.html ./
COPY testdata ./testdata
RUN CGO_ENABLED=0 GOTOOLCHAIN=local GOPROXY=off go test -count=1 ./... && \
    CGO_ENABLED=0 GOTOOLCHAIN=local GOPROXY=off go build -buildvcs=false -trimpath -ldflags="-s -w" -o /trailmix .

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /trailmix /trailmix
USER 10001:10001
ENV GOMEMLIMIT=384MiB GOMAXPROCS=1 PORT=10000
EXPOSE 10000
ENTRYPOINT ["/trailmix"]
