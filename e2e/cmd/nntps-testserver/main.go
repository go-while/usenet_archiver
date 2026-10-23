// nntps-testserver: an NNTPS server for scripts/check_appimage_tls.sh.
//
// It makes a fresh CA and a server certificate for localhost and 127.0.0.1,
// writes the CA certificate to -ca-out (the key stays in memory), and serves
// a group test.group with -articles articles from an nntp-testkit Store
// until SIGINT or SIGTERM. Once it listens it prints "listening ADDR".
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"flag"
	"fmt"
	"log"
	"math/big"
	"net"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-while/nntp-testkit/fakeserver"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:5630", "listen address")
	caOut := flag.String("ca-out", "ca.pem", "where to write the CA certificate (PEM)")
	n := flag.Int("articles", 5, "articles in test.group")
	flag.Parse()

	cert, caPEM, err := certificates()
	if err != nil {
		log.Fatal(err)
	}
	if err := os.WriteFile(*caOut, caPEM, 0o644); err != nil {
		log.Fatal(err)
	}

	st := fakeserver.NewStore()
	st.AddGroup("test.group", "")
	for i := 1; i <= *n; i++ {
		id := fmt.Sprintf("<%d@tls.test>", i)
		st.AddArticle("test.group", fakeserver.Article{
			MessageID: id,
			Headers: []string{
				"Path: fake!not-for-mail",
				"From: Someone <someone@example.com>",
				"Newsgroups: test.group",
				"Subject: tls " + id,
				"Date: Thu, 08 Dec 2016 12:55:04 +0000",
				"Message-ID: " + id,
			},
			Body: []string{fmt.Sprintf("article %d over TLS", i)},
		})
	}
	s, err := fakeserver.Start(fakeserver.Options{
		Addr:     *addr,
		Store:    st,
		Deadline: time.Hour,
		TLS:      &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12},
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("listening", s.Addr())

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	s.Close()
}

// certificates returns a server certificate signed by a new CA, and the CA
// certificate as PEM. Both are valid from an hour ago for a day.
func certificates() (tls.Certificate, []byte, error) {
	now := time.Now()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	caTmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "usenet_archiver AppImage TLS check CA"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	ca, err := x509.ParseCertificate(caDER)
	if err != nil {
		return tls.Certificate{}, nil, err
	}

	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "localhost"},
		DNSNames:     []string{"localhost"},
		IPAddresses:  []net.IP{net.IPv4(127, 0, 0, 1)},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca, &key.PublicKey, caKey)
	if err != nil {
		return tls.Certificate{}, nil, err
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}, caPEM, nil
}
