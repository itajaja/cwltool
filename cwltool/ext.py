import json
import importlib.util
from io import open
import logging
import os
import shutil
import subprocess
import tempfile
import time

from .job import job_output_lock, JobBase, relink_initialworkdir
from .pathmapper import ensure_writable
from .process import get_feature
from .utils import bytes2str_in_dicts

_logger = logging.getLogger("cwltool")


class EXT_SETTINGS(object):
    active = 'CWL_EXT_ACTIVE' in os.environ
    ext_module = os.environ['CWL_EXT_MODULE'] if active else None


class ExtJob(JobBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        spec = importlib.util.spec_from_file_location("cwl_ext_module", EXT_SETTINGS.ext_module)
        if not spec:
            raise ValueError(f'{EXT_SETTINGS.ext_module} is not a python module')

        self.ext_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ext_module)


    def add_volumes(self, pathmapper, volumes):
        host_outdir = self.outdir
        container_outdir = self.builder.outdir
        for src, vol in pathmapper.items():
            if not vol.staged:
                continue
            if vol.target.startswith(container_outdir+"/"):
                host_outdir_tgt = os.path.join(
                    host_outdir, vol.target[len(container_outdir)+1:])
            else:
                host_outdir_tgt = None
            if vol.type in ("File", "Directory"):
                if not vol.resolved.startswith("_:"):
                    volumes.append({
                        'from': vol.resolved,
                        'to': vol.target,
                        'readonly': True,
                    })
            elif vol.type == "WritableFile":
                if self.inplace_update:
                    volumes.append({
                        'from': vol.resolved,
                        'to': vol.target,
                        'readonly': False,
                    })
                else:
                    shutil.copy(vol.resolved, host_outdir_tgt)
                    ensure_writable(host_outdir_tgt)
            elif vol.type == "WritableDirectory":
                if vol.resolved.startswith("_:"):
                    os.makedirs(vol.target, 0o0755)
                else:
                    if self.inplace_update:
                        volumes.append({
                            'from': vol.resolved,
                            'to': vol.target,
                            'readonly': False,
                        })
                    else:
                        shutil.copytree(vol.resolved, host_outdir_tgt)
                        ensure_writable(host_outdir_tgt)
            elif vol.type == "CreateFile":
                if host_outdir_tgt:
                    with open(host_outdir_tgt, "wb") as f:
                        f.write(vol.resolved.encode("utf-8"))
                else:
                    fd, createtmp = tempfile.mkstemp(dir=self.tmpdir)
                    with os.fdopen(fd, "wb") as f:
                        f.write(vol.resolved.encode("utf-8"))
                    volumes.append({
                        'from': createtmp,
                        'to': vol.target,
                        'readonly': False,
                    })

    def schedule(self, payload):
        return self.ext_module.schedule(payload, self)

    def check_finished(self, payload):
        return self.ext_module.check_finished(payload, self)

    def on_finished(self, outputs, status):
        return self.ext_module.on_finished(outputs, status, self)

    def _execute(self, payload, rm_tmpdir=True):
        scr, _ = get_feature(self, "ShellCommandRequirement")

        outputs = {}

        try:
            check_payload = self.schedule(payload)

            rcode = None
            while(rcode is None):
                time.sleep(3)
                rcode = self.check_finished(check_payload)

            if self.successCodes and rcode in self.successCodes:
                processStatus = "success"
            elif self.temporaryFailCodes and rcode in self.temporaryFailCodes:
                processStatus = "temporaryFail"
            elif self.permanentFailCodes and rcode in self.permanentFailCodes:
                processStatus = "permanentFail"
            elif rcode == 0:
                processStatus = "success"
            else:
                processStatus = "permanentFail"

            if self.generatefiles["listing"]:
                relink_initialworkdir(
                    self.generatemapper,
                    self.outdir,
                    self.builder.outdir,
                    inplace_update=self.inplace_update,
                )

            # with open(self.stdout, 'w') as f:
            #     f.write(self.get_log(payload))

            outputs = self.collect_outputs(self.outdir)
            outputs = bytes2str_in_dicts(outputs)

        except Exception:
            _logger.exception("Exception while running job")
            processStatus = "permanentFail"

        if processStatus != "success":
            _logger.warning(u"[job %s] completed %s", self.name, processStatus)
        else:
            _logger.info(u"[job %s] completed %s", self.name, processStatus)

        _logger.debug(u"[job %s] %s", self.name, json.dumps(outputs, indent=4))

        with job_output_lock:
            self.on_finished(outputs, processStatus)
            self.output_callback(outputs, processStatus)

        if self.stagedir and os.path.exists(self.stagedir):
            _logger.debug(
                u"[job %s] Removing input staging directory %s",
                self.name, self.stagedir,
            )
            shutil.rmtree(self.stagedir, True)

        if rm_tmpdir:
            _logger.debug(
                u"[job %s] Removing temporary directory %s",
                self.name, self.tmpdir,
            )
            shutil.rmtree(self.tmpdir, True)

    def run(self, pull_image=True, rm_tmpdir=True, **kwargs):
        if self.stdout:
            _logger.error(
                "[job %s] stdout functionality not available", self.name,
            )
            raise NotImplementedError()
        if self.stdin:
            _logger.error(
                "[job %s] stdin functionality not available", self.name,
            )
            raise NotImplementedError()
        if self.stderr:
            _logger.error(
                "[job %s] stderr functionality not available", self.name,
            )
            raise NotImplementedError()

        (docker_req, docker_is_req) = get_feature(self, "DockerRequirement")

        try:
            img_id = docker_req['dockerPull']
        except KeyError:
            img_id = self.builder.find_default_container()
        # env = None  # type: MutableMapping[Text, Text]
        # env = cast(MutableMapping[Text, Text], os.environ)

        self._setup(kwargs)

        payload = {
            'id': self.name,
            'volumes': [{
                'from': os.path.realpath(self.outdir),
                'to': self.builder.outdir,
                'readonly': False,
            }, {
                'from': os.path.realpath(self.tmpdir),
                'to': '/tmp',
                'readonly': False,
            }],
            'workdir': self.builder.outdir,
            'stdout': self.stdout,
            'env': {
                'TMPDIR': '/tmp',
                'HOME': self.builder.outdir,
                **self.environment,
            },
            'img_id': img_id,
            'command_line': self.command_line,
        }

        self.add_volumes(self.pathmapper, payload['volumes'])
        if self.generatemapper:
            self.add_volumes(self.generatemapper, payload['volumes'])

        self._execute(payload, rm_tmpdir=rm_tmpdir)
