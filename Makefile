# Packaging targets. Application code: usenet_archiver/. Zipapp: scripts/build_zipapp.sh

.PHONY: zipapp clean-zipapp appimage clean-appimage test e2e gui

zipapp:
	./scripts/build_zipapp.sh

clean-zipapp:
	rm -rf .zipapp_stage dist/usenet-archiver dist/usenet-archiver-*-zipapp.tar.gz

appimage: zipapp
	./scripts/build_appimage.sh

clean-appimage:
	rm -rf .appimage_stage dist/*.AppImage

test:
	python3 -m unittest discover -s tests -v

# End-to-end: fake NNTP server (nntp-testkit) -> CLI -> mbox -> go-lib-mbox2nntp. Needs Go.
e2e:
	cd e2e && go test -count=1 ./...

gui:
	python3 -m usenet_archiver
