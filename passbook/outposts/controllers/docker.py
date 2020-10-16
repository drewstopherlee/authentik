"""Docker controller"""
from typing import Dict, Tuple

from docker import DockerClient, from_env
from docker.errors import DockerException, NotFound
from docker.models.containers import Container
from yaml import safe_dump

from passbook import __version__
from passbook.outposts.controllers.base import BaseController, ControllerException
from passbook.outposts.models import Outpost


class DockerController(BaseController):
    """Docker controller"""

    client: DockerClient

    container: Container

    image_base = "beryju/passbook"

    def __init__(self, outpost: Outpost) -> None:
        super().__init__(outpost)
        self.client = from_env()

    def _get_env(self) -> Dict[str, str]:
        return {
            "PASSBOOK_HOST": self.outpost.config.passbook_host,
            "PASSBOOK_INSECURE": str(self.outpost.config.passbook_host_insecure),
            "PASSBOOK_TOKEN": self.outpost.token.token_uuid.hex,
        }

    def _comp_env(self, container: Container) -> bool:
        """Check if container's env is equal to what we would set. Return true if container needs
        to be rebuilt."""
        should_be = self._get_env()
        container_env = container.attrs.get("Config", {}).get("Env", {})
        for key, expected_value in should_be.items():
            if key not in container_env:
                continue
            if container_env[key] != expected_value:
                return True
        return False

    def _get_container(self) -> Tuple[Container, bool]:
        container_name = f"passbook-proxy-{self.outpost.uuid.hex}"
        try:
            return self.client.containers.get(container_name), False
        except NotFound:
            self.logger.info("Container does not exist, creating")
            image_name = f"{self.image_base}-{self.outpost.type}:{__version__}"
            self.client.images.pull(image_name)
            return (
                self.client.containers.create(
                    image=image_name,
                    name=f"passbook-proxy-{self.outpost.uuid.hex}",
                    detach=True,
                    ports={x: x for _, x in self.deployment_ports.items()},
                    environment=self._get_env(),
                ),
                True,
            )

    def run(self):
        try:
            container, has_been_created = self._get_container()
            if has_been_created:
                return None
            # Check if the container is out of date, delete it and retry
            if len(container.image.tags) > 0:
                tag: str = container.image.tags[0]
                _, _, version = tag.partition(":")
                if version != __version__:
                    self.logger.info(
                        "Container has mismatched version, re-creating...",
                        has=version,
                        should=__version__,
                    )
                    container.kill()
                    container.remove(force=True)
                    return self.run()
            # Check that container values match our values
            if self._comp_env(container):
                self.logger.info("Container has outdated config, re-creating...")
                container.kill()
                container.remove(force=True)
                return self.run()
            # Check that container is healthy
            if (
                container.status == "running"
                and container.attrs.get("State", {}).get("Health", {}).get("Status", "")
                != "healthy"
            ):
                # At this point we know the config is correct, but the container isn't healthy,
                # so we just restart it with the same config
                self.logger.info("Container is unhealthy, restarting...")
                container.restart()
            # Check that container is running
            if container.status != "running":
                self.logger.info("Container is not running, restarting...")
                container.start()
            return None
        except DockerException as exc:
            raise ControllerException from exc

    def get_static_deployment(self) -> str:
        """Generate docker-compose yaml for proxy, version 3.5"""
        ports = [f"{x}:{x}" for _, x in self.deployment_ports.items()]
        compose = {
            "version": "3.5",
            "services": {
                f"passbook_{self.outpost.type}": {
                    "image": f"{self.image_base}-{self.outpost.type}:{__version__}",
                    "ports": ports,
                    "environment": {
                        "PASSBOOK_HOST": self.outpost.config.passbook_host,
                        "PASSBOOK_INSECURE": str(
                            self.outpost.config.passbook_host_insecure
                        ),
                        "PASSBOOK_TOKEN": self.outpost.token.token_uuid.hex,
                    },
                }
            },
        }
        return safe_dump(compose, default_flow_style=False)
