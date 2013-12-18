from docker.client import APIError
import logging
import re

log = logging.getLogger(__name__)


class BuildError(Exception):
    pass


class Service(object):
    def __init__(self, name, client=None, links=[], **options):
        if not re.match('^[a-zA-Z0-9_]+$', name):
            raise ValueError('Invalid name: %s' % name)
        if 'image' in options and 'build' in options:
            raise ValueError('Service %s has both an image and build path specified. A service can either be built to image or use an existing image, not both.' % name)

        self.name = name
        self.client = client
        self.links = links or []
        self.options = options

    @property
    def containers(self):
        return list(self.get_containers(all=True))

    def get_containers(self, all):
        for container in self.client.containers(all=all):
            name = get_container_name(container)
            if is_valid_name(name) and parse_name(name)[0] == self.name:
                yield container

    def start(self):
        if len(self.containers) == 0:
            return self.start_container()

    def stop(self):
        self.scale(0)

    def scale(self, num):
        while len(self.containers) < num:
            self.start_container()

        while len(self.containers) > num:
            self.stop_container()

    def create_container(self, **override_options):
        """
        Create a container for this service. If the image doesn't exist, attempt to pull
        it.
        """
        container_options = self._get_container_options(override_options)
        try:
            return self.client.create_container(**container_options)
        except APIError, e:
            if e.response.status_code == 404 and e.explanation and 'No such image' in e.explanation:
                log.info('Pulling image %s...' % container_options['image'])
                self.client.pull(container_options['image'])
                return self.client.create_container(**container_options)
            raise

    def start_container(self, container=None, **override_options):
        if container is None:
            container = self.create_container(**override_options)
        port_bindings = {}
        for port in self.options.get('ports', []):
            port = unicode(port)
            if ':' in port:
                internal_port, external_port = port.split(':', 1)
                port_bindings[int(internal_port)] = int(external_port)
            else:
                port_bindings[int(port)] = None
        log.info("Starting %s..." % container['Id'])
        self.client.start(
            container['Id'],
            links=self._get_links(),
            port_bindings=port_bindings,
        )
        return container

    def stop_container(self):
        container = self.containers[-1]
        log.info("Stopping and removing %s..." % get_container_name(container))
        self.client.kill(container)
        self.client.remove_container(container)

    def next_container_number(self):
        numbers = [parse_name(get_container_name(c))[1] for c in self.containers]

        if len(numbers) == 0:
            return 1
        else:
            return max(numbers) + 1

    def get_names(self):
        return [get_container_name(c) for c in self.containers]

    def inspect(self):
        return [self.client.inspect_container(c['Id']) for c in self.containers]

    def _get_links(self):
        links = {}
        for service in self.links:
            for name in service.get_names():
                links[name] = name
        return links

    def _get_container_options(self, override_options):
        keys = ['image', 'command', 'hostname', 'user', 'detach', 'stdin_open', 'tty', 'mem_limit', 'ports', 'environment', 'dns', 'volumes', 'volumes_from']
        container_options = dict((k, self.options[k]) for k in keys if k in self.options)
        container_options.update(override_options)

        number = self.next_container_number()
        container_options['name'] = make_name(self.name, number)

        if 'ports' in container_options:
            container_options['ports'] = [unicode(p).split(':')[0] for p in container_options['ports']]

        if 'build' in self.options:
            container_options['image'] = self.build()

        return container_options

    def build(self):
        log.info('Building %s from %s...' % (self.name, self.options['build']))

        build_output = self.client.build(self.options['build'], stream=True)

        image_id = None

        for line in build_output:
            if line:
                match = re.search(r'Successfully built ([0-9a-f]+)', line)
                if match:
                    image_id = match.group(1)
            print line

        if image_id is None:
            raise BuildError()

        return image_id


name_regex = '^(.+)_(\d+)$'


def make_name(prefix, number):
    return '%s_%s' % (prefix, number)


def is_valid_name(name):
    return (re.match(name_regex, name) is not None)


def parse_name(name):
    match = re.match(name_regex, name)
    (service_name, suffix) = match.groups()
    return (service_name, int(suffix))


def get_container_name(container):
    # inspect
    if 'Name' in container:
        return container['Name']
    # ps
    for name in container['Names']:
        if len(name.split('/')) == 2:
            return name[1:]