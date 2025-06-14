#!/usr/bin/env python
"""
Extension of the networkx.DiGraph to allow easy synthesis
and network specific annotations.
"""

from itertools import count
import random
import ipaddress
import enum
import networkx as nx
# from past.builtins import basestring

from tekton.bgp import ASPathList
from tekton.bgp import CommunityList
from tekton.bgp import RouteMap
from tekton.bgp import IpPrefixList
from tekton.utils import is_empty
from tekton.utils import is_symbolic
from tekton.utils import VALUENOTSET


__author__ = "Ahmed El-Hassany"
__email__ = "a.hassany@gmail.com"


VERTEX_TYPE = 'VERTEX_TYPE'
EDGE_TYPE = 'EDGE_TYPE'


def is_valid_add(addr, allow_not_set=True):
    """Return True if the address is valid"""
    if allow_not_set and is_empty(addr):
        return True
    return isinstance(addr, (ipaddress.IPv4Interface, ipaddress.IPv6Interface))


def is_bool_or_notset(value):
    return value in [True, False, VALUENOTSET]


class VERTEXTYPE(enum.Enum):
    """Enum for VERTEX types in the network graph"""
    ROUTER = 'ROUTER'
    NETWORK = 'NETWORK'
    PEER = 'PEER'


class EDGETYPE(enum.Enum):
    """Enum for Edge types in the network graph"""
    ROUTER = 'ROUTER_EDGE'
    NETWORK = 'NETWORK_EDGE'
    PEER = 'PEER_EDGE'


class OSPFNetworkType(enum.Enum):
    """
    The network type for ospf interfaces
    """
    broadcast = 'broadcast'  # Specify OSPF broadcast multi-access network
    non_broadcast = 'non-broadcast'  # Specify OSPF NBMA network
    point_to_multipoint = 'point-to-multipoint'  # Specify OSPF point-to-multipoint network
    point_to_point = 'point-to-point'  # Specify OSPF point-to-point network


class NetworkGraph(nx.DiGraph):
    """
    An extended version of networkx.DiGraph
    """

    def __init__(self, graph=None):
        assert not graph or isinstance(graph, nx.DiGraph)
        super(NetworkGraph, self).__init__(graph)
        self._counter = count(1)

    def add_node(self, n, **attr):
        """
        Add a single node n and update node attributes.
        Inherits networkx.DiGraph.add_node
        Just check that VERTEX_TYPE is defined.
        :param n: node name (str)
        :param attr: dict of attributes
        :return: None
        """
        type_set = False
        if attr and VERTEX_TYPE in attr:
            type_set = True
        if not type_set:
            raise ValueError('Cannot add directly nodes, must use add_router, add_peer etc..')
        super(NetworkGraph, self).add_node(n, **attr)

    def add_router(self, router):
        """
        Add a new router to the graph
        the node in the graph will be annotated with VERTEX_TYPE=NODE_TYPE
        :param router: the name of the router
        :return: None
        """
        self.add_node(router, **{VERTEX_TYPE: VERTEXTYPE.ROUTER})

    def add_peer(self, router):
        """
        Add a new router to the graph, this router is special only to model external routers
        the node in the graph will be annotated with VERTEXTYPE.PEER
        :param router: the name of the router
        :return: None
        """
        self.add_node(router, **{VERTEX_TYPE: VERTEXTYPE.PEER})

    def add_network(self, network):
        """
        Add a new network to the graph
        the node in the graph will be annotated with VERTEX_TYPE=VERTEXTYPE.NETWORK
        :param router: the name of the router
        :return: None
        """
        self.add_node(network, **{VERTEX_TYPE: VERTEXTYPE.NETWORK})

    def is_peer(self, node):
        """
        Checks if a given node is a Peer
        :param node: node name, must be in G
        :return: True if a node is a peering network
        """
        if not self.has_node(node):
            return False
        #print('R9!!!', node, self.node[node], self.node[node].keys())
        return self._node[node][VERTEX_TYPE] == VERTEXTYPE.PEER

    def is_local_router(self, node):
        """
        Checks if a given node is local router under the administrative domain
        (i.e., not a peering network)
        :param node: node name, must be in G
        :return: True if a node is part of the administrative domain
        """
        if not self.has_node(node):
            return False
        return self._node[node][VERTEX_TYPE] == VERTEXTYPE.ROUTER

    def is_network(self, node):
        """Node is a just a subnet (not a router)"""
        if not self.has_node(node):
            return False
        return self._node[node][VERTEX_TYPE] == VERTEXTYPE.NETWORK

    def is_router(self, node):
        """True for Nodes and Peers"""
        return self.is_peer(node) or self.is_local_router(node)

    def local_routers_iter(self):
        """Iterates over local routers"""
        for node in self.nodes():
            if self.is_local_router(node):
                yield node

    def peers_iter(self):
        """Iterates over peers"""
        for node in self.nodes():
            if self.is_peer(node):
                yield node

    def routers_iter(self):
        """Iterates over routers (local or peers)"""
        for node in self.nodes():
            if self.is_router(node):
                yield node

    def networks_iter(self):
        """Iterates over networks"""
        for node in self.nodes():
            if self.is_network(node):
                yield node

    def add_edge(self, u, v, **attr):
        """
        Add an edge u,v to G.
        Inherits networkx.DiGraph.add_Edge
        Just check that EDGE_TYPE is defined.
        :param u:
        :param v:
        :param attr:
        :return:
        """
        type_set = False
        if attr and EDGE_TYPE in attr:
            type_set = True
        if not type_set:
            msg = 'Cannot add directly edges, must use ' \
                  'add_router_edge, add_peer_edge etc..'
            raise ValueError(msg)
        super(NetworkGraph, self).add_edge(u, v, **attr)

    def add_router_edge(self, u, v, **attr):
        """
        Add an edge between two routers
        :param u: source router
        :param v: dst routers
        :param attr: attributes
        :return: None
        """
        assert self.is_local_router(u), "Source '%s' is not a router" % u
        assert self.is_local_router(u), "Destination '%s' is not a router" % u
        attr[EDGE_TYPE] = EDGETYPE.ROUTER
        self.add_edge(u, v,  **attr)

    def is_local_router_edge(self, src, dst):
        """Return True if the two local routers are connected"""
        if not self.has_edge(src, dst):
            return False
        return self[src][dst][EDGE_TYPE] == EDGETYPE.ROUTER

    def add_peer_edge(self, u, v, **attr):
        """
        Add an edge between two routers (one local and the other is a peer)
        :param u: source router
        :param v: dst routers
        :param attr: attributes
        :return: None
        """
        #print('u', u, v)
        err1 = "One side is not a peer router (%s, %s)" % (u, v)
        assert self.is_peer(u) or self.is_peer(v), err1
        err2 = "One side is not a local router (%s, %s)" % (u, v)
        assert self.is_router(u) or self.is_router(v), err2
        attr[EDGE_TYPE] = EDGETYPE.PEER
        self.add_edge(u, v, **attr)

    def add_network_edge(self, u, v, **attr):
        """
        Add an edge between a router and a network
        :param u: source router or network
        :param v: dst routers r network
        :param attr: attributes
        :return: None
        """
        err1 = "One side is not a local router (%s, %s)" % (u, v)
        assert self.is_router(u) or self.is_router(v), err1
        err2 = "One side is not a network (%s, %s)" % (u, v)
        assert self.is_network(u) or self.is_network(v), err2
        attr[EDGE_TYPE] = EDGETYPE.NETWORK
        self.add_edge(u, v, **attr)

    def get_loopback_interfaces(self, node):
        """
        Returns the dict of the loopback interfaces
        will set empty dict if it doesn't exists
        :param node: the name of the node
        :return: a dict
        """
        if 'loopbacks' not in self._node[node]:
            self._node[node]['loopbacks'] = {}
        return self._node[node]['loopbacks']

    def is_loopback(self, node, iface):
        """Return true if iface is defined as a loopback"""
        err = "Node {} is not defined as router".format(node)
        assert self.is_router(node), err
        return iface in self.get_loopback_interfaces(node)

    def set_loopback_addr(self, node, loopback, addr):
        """
        Assigns an IP address to a loopback interface
        :param node: name of the router
        :param loopback: name of loopback interface. e.graph., lo0, lo1, etc..
        :param addr: an instance of ipaddress.IPv4Interface or ipaddress.IPv6Interface
        :return: None
        """
        assert is_valid_add(addr)
        loopbacks = self.get_loopback_interfaces(node)
        if loopback not in loopbacks:
            loopbacks[loopback] = {}
        assert is_empty(loopbacks[loopback].get('addr', None)), loopbacks[loopback].get('addr', None)
        loopbacks[loopback]['addr'] = addr

    def get_loopback_addr(self, node, loopback):
        """
        Gets the IP address of a loopback interface
        :param graph: network graph (networkx.DiGraph)
        :param node: name of the router
        :param loopback: name of loopback interface. e.graph., lo0, lo1, etc..
        :return: an instance of ipaddress.IPv4Interface or ipaddress.IPv6Interface
        """
        addr = self._node[node].get('loopbacks', {}).get(loopback, {}).get('addr', None)
        err = "IP Address is not assigned for loopback'%s'-'%s'" % (node, loopback)
        assert addr, err
        return addr

    def set_loopback_description(self, node, loopback, description):
        """
        Assigns some help text to the interface
        :param node: name of the router
        :param loopback: name of loopback interface. e.graph., lo0, lo1, etc..
        :return: None
        """
        self._node[node]['loopbacks'][loopback]['description'] = description

    def get_loopback_description(self, node, loopback):
        """
        Return the help text to the interface or None
        :param node: name of the router
        :param loopback: name of loopback interface. e.graph., lo0, lo1, etc..
        :return: text or none
        """
        return self._node[node]['loopbacks'][loopback].get('description', None)

    def get_ifaces(self, node):
        if 'ifaces' not in self._node[node]:
            self._node[node]['ifaces'] = {}
        return self._node[node]['ifaces']

    def add_iface(self, node, iface_name, is_shutdown=True):
        """
        Add an interface to a router
        :param node: name of the router
        :param iface_name:
        :return:
        """
        assert self.is_router(node)
        assert is_bool_or_notset(is_shutdown)
        ifaces = self.get_ifaces(node)
        assert iface_name not in ifaces, "%s in %s" % (iface_name, ifaces.keys())
        ifaces[iface_name] = {'shutdown': is_shutdown,
                              'addr': None,
                              'description': None}

    def is_interface(self, node, iface):
        """Return true if iface is defined as a physical interface"""
        err = "Node {} is not defined as router".format(node)
        assert self.is_router(node), err
        return iface in self.get_ifaces(node)

    def set_iface_addr(self, node, iface_name, addr):
        """Return set the address of an interface or None"""
        assert self.is_router(node)
        assert is_valid_add(addr)
        ifaces = self.get_ifaces(node)
        err = "Undefined iface '%s' in %s" % (iface_name, ifaces.keys())
        assert iface_name in ifaces, err
        ifaces[iface_name]['addr'] = addr

    def get_iface_addr(self, node, iface_name):
        """Return the address of an interface or None"""
        assert self.is_router(node)
        ifaces = self.get_ifaces(node)
        err = "Undefined iface '%s' in %s" % (iface_name, ifaces.keys())
        assert iface_name in ifaces, err
        return ifaces[iface_name]['addr']

    def set_iface_description(self, node, iface_name, description):
        """Assigns some help text to the interface"""
        assert self.is_router(node)
        ifaces = self.get_ifaces(node)
        err = "Undefined iface '%s' in %s" % (iface_name, ifaces.keys())
        assert iface_name in ifaces, err
        ifaces[iface_name]['description'] = description

    def get_iface_description(self, node, iface_name):
        """Get help text to the interface (if exists)"""
        assert self.is_router(node)
        ifaces = self.get_ifaces(node)
        err = "Undefined iface '%s' in %s" % (iface_name, ifaces.keys())
        assert iface_name in ifaces, err
        return ifaces[iface_name]['description']

    def is_iface_shutdown(self, node, iface_name):
        """Return True if the interface is set to be shutdown"""
        assert self.is_router(node)
        ifaces = self.get_ifaces(node)
        err = "Undefined iface '%s' in %s" % (iface_name, ifaces.keys())
        assert iface_name in ifaces, err
        return ifaces[iface_name]['shutdown']

    def set_iface_shutdown(self, node, iface_name, is_shutdown):
        """Set True if the interface is set to be shutdown"""
        assert self.is_router(node)
        assert is_bool_or_notset(is_shutdown)
        ifaces = self.get_ifaces(node)
        err = "Undefined iface '%s' in %s" % (iface_name, ifaces.keys())
        assert iface_name in ifaces, err
        ifaces[iface_name]['shutdown'] = is_shutdown

    def get_interface_loop_addr(self, node, iface):
        """Get the address of an interface or a loopback"""
        if self.is_interface(node, iface):
            return self.get_iface_addr(node, iface)
        elif self.is_loopback(node, iface):
            return self.get_loopback_addr(node, iface)
        else:
            raise ValueError("Not valid interface/loopback {} at router {}".
                             format(node, iface))

    def set_edge_iface(self, src, dst, iface):
        """
        Assigns an interface name to the outgoing edge, e.graph., f0/0, f1/0, etc..
        :param src: name of the source router (the one that will change)
        :param dst: name of the destination router
        :param iface: the name of the the interface
        :return: None
        """
        self[src][dst]['iface'] = iface

    def get_edge_iface(self, src, dst):
        """
        Gets the interface name to the outgoing edge, e.graph., f0/0, f1/0, etc..
        :param src: name of the source router (the one that will change)
        :param dst: name of the destination router
        :return the name of the the interface
        """
        return self[src][dst].get('iface', None)

    def get_static_routes(self, node):
        """Return a dict of configured static routes or VALUENOTSET"""
        assert self.is_router(node)
        if 'static' not in self._node[node]:
            self._node[node]['static'] = {}
        return self._node[node]['static']

    def add_static_route(self, node, prefix, next_hop):
        """
        Set a static route
        :param node: Router
        :param prefix: Prefixed to be routed
        :param next_hop: Router
        :return:
        """
        attrs = self.get_static_routes(node)
        if is_empty(attrs):
            self._node[node]['static'] = {}
        self._node[node]['static'][prefix] = next_hop

    def get_acls(self, node):
        """Return a dict of ACLs"""
        assert self.is_router(node)
        if 'acls' not in self._node[node]:
            self._node[node]['acls'] = {}
        return self._node[node]['acls']

    def add_acl(self, node, interface, inout, acl_number=None):
        """
        add Access_List
        :param node: Router
        :param interface: interface name
        :param inout: 'in' or 'out'
        """
        attrs = self.get_acls(node)
        if is_empty(attrs):
            self._node[node]['acls'] ={}
        if interface not in self._node[node]['acls']:
            self._node[node]['acls'][interface] = {}
        if not acl_number:
            if not self._node[node]['acls'][interface]:
                acl_number = random.randint(0,20)
            else:
                max_num = max(self._node[node]['acls'][interface], key=self._node[node]['acls'][interface].get)
                acl_number = max_num + 1
        self._node[node]['acls'][interface][acl_number] = [inout]
        return acl_number

    def add_acl_entry(self, node, interface, acl_number, item):
        """
        Add Access_List Entry
        """
        if item not in self._node[node]['acls'][interface][acl_number]:
            self._node[node]['acls'][interface][acl_number].append(item)


    def set_acls_empty(self, node):
        self._node[node]['acls'] = VALUENOTSET

    def set_static_routes_empty(self, node):
        """
        Set static routes to VALUENOTSET allowing the
        synthesizer to generate as many static routes
        """
        self._node[node]['static'] = VALUENOTSET

    def enable_ospf(self, node, process_id=100):
        """
        Enable OSPF at a given router
        :param node: local router
        :param process_id: integer
        :return: None
        """
        err = "Cannot enable OSPF on router {}".format(node)
        assert self.is_local_router(node), err
        self._node[node]['ospf'] = dict(process_id=process_id, networks={})

    def get_ospf_process_id(self, node):
        """Return the OSPF process ID"""
        err1 = "OSPF is not enabled on router: {}".format(node)
        assert self.is_ospf_enabled(node), err1
        return self._node[node]['ospf']['process_id']

    def is_ospf_enabled(self, node):
        """
        Return True if the node has OSPF process
        :param node: local router
        :return: bool
        """
        if not self.is_local_router(node):
            return False
        return 'ospf' in self._node[node]

    def get_ospf_networks(self, node):
        """
        Return a dict of Announced networks in OSPF at the router
        :param node: local router
        :return: dict Network->Area
        """
        err1 = "OSPF is not enabled on router: {}".format(node)
        assert self.is_ospf_enabled(node), err1
        return self._node[node]['ospf']['networks']

    def add_ospf_network(self, node, network, area):
        """
        :param node:
        :param network:
        :param area:
        :return: None
        """
        networks = self.get_ospf_networks(node)
        networks[network] = area

    def set_edge_ospf_cost(self, src, dst, cost):
        """
        Set the OSPF cost of an edge
        :param src: OSPF enabled local router
        :param dst: OSPF enabled local router
        :param cost: int or VALUENOTSET
        :return: None
        """
        err1 = "OSPF is not enabled on router: {}".format(src)
        err2 = "OSPF is not enabled on router: {}".format(dst)
        assert self.is_ospf_enabled(src), err1
        assert self.is_ospf_enabled(dst), err2
        self[src][dst]['ospf_cost'] = cost

    def get_edge_ospf_cost(self, src, dst):
        """
        Get the OSPF cost of an edge
        :param src: OSPF enabled local router
        :param dst: OSPF enabled local router
        :return: None, VALUENOTSET, int
        """
        err1 = "OSPF is not enabled on router: {}".format(src)
        err2 = "OSPF is not enabled on router: {}".format(dst)
        assert self.is_ospf_enabled(src), err1
        assert self.is_ospf_enabled(dst), err2
        return self[src][dst].get('ospf_cost', None)

    def set_ospf_interface_network_type(self, node, iface, network_type):
        """
        Set the OSPF Network type of the given interface
        See OSPFNetworkType
        """
        err1 = "OSPF is not enabled on router: {}".format(node)
        assert self.is_ospf_enabled(node), err1
        is_iface = iface in self.get_ifaces(node)
        is_loop = iface in self.get_loopback_interfaces(node)
        err2 = "Interface doesn't exist: {}:{}".format(node, iface)
        assert is_iface or is_loop, err2
        assert isinstance(network_type, OSPFNetworkType)
        if is_iface:
            ifaces = self.get_ifaces(node)
        else:
            ifaces = self.get_loopback_interfaces(node)
        ifaces[iface]['network_type'] = network_type

    def get_ospf_interface_network_type(self, node, iface):
        """
        Get the OSPF Network type of the given interface
        See OSPFNetworkType
        """
        err1 = "OSPF is not enabled on router: {}".format(node)
        assert self.is_ospf_enabled(node), err1
        is_iface = iface in self.get_ifaces(node)
        is_loop = iface in self.get_loopback_interfaces(node)
        err2 = "Interface doesn't exist: {}:{}".format(node, iface)
        assert is_iface or is_loop, err2
        if is_iface:
            ifaces = self.get_ifaces(node)
        else:
            ifaces = self.get_loopback_interfaces(node)
        return ifaces[iface].get('network_type', None)

    def get_bgp_attrs(self, node):
        """Return a dict of all BGP related attrs given to a node"""
        assert self.is_router(node), "Node is not a router {}".format(node)
        if 'bgp' not in self._node[node]:
            self._node[node]['bgp'] = {'asnum': None,
                                      'neighbors': {},
                                      'announces': {}}
        return self._node[node]['bgp']

    def set_bgp_asnum(self, node, asnum):
        """Sets the AS number of a given router"""
        assert isinstance(asnum, int)
        self.get_bgp_attrs(node)['asnum'] = asnum

    def is_bgp_enabled(self, node):
        """Return True if the router has BGP configurations"""
        return self.get_bgp_asnum(node) is not None

    def get_bgp_asnum(self, node):
        """Get the AS number of a given router"""
        return self.get_bgp_attrs(node).get('asnum', None)

    def get_bgp_neighbors(self, node):
        """Get a dictionary of BGP peers"""
        return self.get_bgp_attrs(node).get('neighbors', None)

    def set_bgp_router_id(self, node, router_id):
        """Sets the BGP router ID of a given router"""
        assert self.is_bgp_enabled(node)
        if not is_empty(router_id) and not is_symbolic(router_id):
            assert isinstance(router_id, (int, ipaddress.IPv4Address))
            if isinstance(router_id, int):
                assert router_id > 0
        self.get_bgp_attrs(node)['router_id'] = router_id

    def get_bgp_router_id(self, node):
        """Get the BGP router ID of a given router"""
        # TODO: Also read the interface addresses in case no explict ID set
        assert self.is_bgp_enabled(node)
        return self.get_bgp_attrs(node).get('router_id', None)

    def add_bgp_neighbor(self, router_a, router_b, router_a_iface=None, router_b_iface=None, description=None):
        """
        Add BGP peer
        Peers are added by their name in the graph
        :param router_a: Router name
        :param router_b: Router name
        :param router_a_iface: The peering interface can be
                        VALUENOTSET, Physical Iface, loop back.
        :param router_b_iface: The peering interface
        :param description:
        :return:
        """
        if not router_a_iface:
            router_a_iface = VALUENOTSET
        if not router_b_iface:
            router_b_iface = VALUENOTSET
        neighbors_a = self.get_bgp_neighbors(router_a)
        neighbors_b = self.get_bgp_neighbors(router_b)
        err1 = "Router %s already has BGP neighbor %s configured" % (router_a, router_b)
        assert router_b not in neighbors_a, err1
        err2 = "Router %s already has BGP neighbor %s configured" % (router_b, router_a)
        assert router_a not in neighbors_b, err2
        neighbors_a[router_b] = {'peering_iface': router_b_iface}
        neighbors_b[router_a] = {'peering_iface': router_a_iface}
        if not description:
            desc1 = 'To %s' % router_b
            desc2 = 'To %s' % router_a
            self.set_bgp_neighbor_description(router_a, router_b, desc1)
            self.set_bgp_neighbor_description(router_b, router_a, desc2)
        else:
            self.set_bgp_neighbor_description(router_a, router_b, description)
            self.set_bgp_neighbor_description(router_b, router_a, description)

    def set_bgp_neighbor_iface(self, node, neighbor, iface):
        """Set the interface to which the peering session to be established"""
        neighbors = self.get_bgp_neighbors(node)
        assert neighbor in neighbors, neighbors.keys()
        is_iface = iface in self.get_ifaces(neighbor)
        is_loopback = iface in self.get_loopback_interfaces(neighbor)
        err = "Cannot peer node {} with node {}:{}. " \
              "The interface {} is not attached to neighbor {}".format(
            node, neighbor, iface, iface, neighbor)
        assert is_iface or is_loopback, err
        neighbors[neighbor]['peering_iface'] = iface

    def get_bgp_neighbor_iface(self, node, neighbor):
        """Get the interface to which the peering session to be established"""
        neighbors = self.get_bgp_neighbors(node)
        assert neighbor in neighbors
        return neighbors[neighbor]['peering_iface']

    def set_bgp_neighbor_description(self, node, neighbor, description):
        """Returns text description for help about the neighbor"""
        assert isinstance(description, str)
        # Cisco's limit
        assert len(description) <= 80
        self.get_bgp_neighbors(node)[neighbor]['description'] = description

    def get_bgp_neighbor_description(self, node, neighbor):
        """Returns text description for help about the neighbor"""
        return self.get_bgp_neighbors(node)[neighbor].get('description')

    def assert_valid_neighbor(self, node, neighbor):
        neighbors = self.get_bgp_neighbors(node)
        err = "Not not valid BGP neighbor %s->%s" % (node, neighbor)
        assert neighbor in neighbors, err

    def get_bgp_neighbor_remoteas(self, node, neighbor):
        """Get the AS number of a BGP peer (by name)"""
        self.assert_valid_neighbor(node, neighbor)
        return self.get_bgp_asnum(neighbor)

    def get_bgp_advertise(self, node):
        """
        Returns a list of advertisements announced by a peer
        :param node: router
        :return: list
        """
        #assert self.is_peer(node)
        name = 'advertise'
        attrs = self.get_bgp_attrs(node)
        #print('attrs', attrs)
        if name not in attrs:
            attrs['advertise'] = {}
        return attrs['advertise']

    def add_bgp_advertise(self, node, announcement, loopback=None):
        """
        Add an advertisement by an external peer
        """
        self.get_bgp_advertise(node)[announcement] = {'loopback': loopback}

    def get_bgp_announces(self, node):
        """
        Returns a dict of announcements to be made by the node
        :param node: router
        :return: dic
        """
        return self.get_bgp_attrs(node)['announces']

    def add_bgp_announces(self, node, network, route_map_name=None):
        """
        Router to announce a given network over BGP
        :param node:  router
        :param network: either a Loopback, a network name, or actual ipaddress.ip_network
        :param route_map: Route-map to modify the attributes
        :return: None
        """
        is_network = self.has_node(network) and self.is_network(network)
        is_lo = network in self.get_loopback_interfaces(node)
        is_net = isinstance(network, (ipaddress.IPv4Network, ipaddress.IPv6Network))
        assert is_network or is_lo or is_net
        announcements = self.get_bgp_announces(node)
        announcements[network] = {}
        if route_map_name:
            assert route_map_name in self.get_route_maps(node)
            announcements[network]['route_map'] = route_map_name

    def get_bgp_communities_list(self, node):
        """
        Return communities list registered on the router
        :param node: the router on which the list resides
        :return: {} or a dict of communities list
        """
        comm_list = self.get_bgp_attrs(node).get('communities-list', None)
        if comm_list is None:
            self._node[node]['bgp']['communities-list'] = {}
            comm_list = self._node[node]['bgp']['communities-list']
        return comm_list

    def add_bgp_community_list(self, node, community_list):
        """
        Add a community list
        :param node: the router on which the list resides
        :param community_list: instance of CommunityList
        :return: CommunityList
        """
        #print('community_list', community_list, node)
        assert isinstance(community_list, CommunityList)
        lists = self.get_bgp_communities_list(node)
        #print('node', node, 'lists', lists)
        if community_list.list_id is None:
            #print('true')
            list_id = None
            while list_id is None or list_id in lists:
                list_id = next(self._counter)
            community_list._list_id = list_id

        #print('community_list.list_id', community_list.list_id, lists)
        #assert community_list.list_id not in lists, "List exists %s" % community_list.list_id
        if community_list.list_id not in lists:
            lists[community_list.list_id] = community_list
        return community_list

    def del_community_list(self, node, community_list):
        list_id = getattr(community_list, 'list_id', community_list)
        lists = self.get_bgp_communities_list(node)
        del lists[list_id]

    def get_as_path_list(self, node):
        """
        Return as paths list registered on the router
        :param node: the router on which the list resides
        :return: {} or a dict of communities list
        """
        as_paths_list = self.get_bgp_attrs(node).get('as-path-list', None)
        if as_paths_list is None:
            self._node[node]['bgp']['as-path-list'] = {}
            as_paths_list = self._node[node]['bgp']['as-path-list']
        return as_paths_list

    def add_as_path_list(self, node, as_path_list):
        """
        Add a As path_list list
        :param node: the router on which the list resides
        :param as_path_list: instance of CommunityList
        :return: CommunityList
        """
        assert isinstance(as_path_list, ASPathList)
        lists = self.get_as_path_list(node)
        assert as_path_list.list_id not in lists
        lists[as_path_list.list_id] = as_path_list
        return as_path_list

    def add_bgp_import_route_map(self, node, neighbor, route_map_name):
        """
        Specifies the import route map from the given neighbor
        :param node:
        :param neighbor:
        :param route_map_name:
        :return: None
        """
        assert route_map_name in self.get_route_maps(node), \
            "Route map is not defiend %s" % route_map_name
        neighbors = self.get_bgp_neighbors(node)
        self.assert_valid_neighbor(node, neighbor)
        neighbors[neighbor]['import_map'] = route_map_name

    def get_bgp_import_route_map(self, node, neighbor):
        """
        Get the name of the import route map from the given neighbor
        :param node:
        :param neighbor:
        :return: route_map_name
        """
        neighbors = self.get_bgp_neighbors(node)
        self.assert_valid_neighbor(node, neighbor)
        route_map_name = neighbors[neighbor].get('import_map', None)
        if not route_map_name:
            return None
        assert route_map_name in self.get_route_maps(node), \
            "Route map is not defiend %s" % route_map_name
        return route_map_name

    def add_bgp_export_route_map(self, node, neighbor, route_map_name):
        """
        Specifies the export route map to the given neighbor
        :param node:
        :param neighbor:
        :param route_map_name:
        :return: None
        """
        assert route_map_name in self.get_route_maps(node), \
            "Route map is not defiend %s" % route_map_name
        neighbors = self.get_bgp_neighbors(node)
        self.assert_valid_neighbor(node, neighbor)
        neighbors[neighbor]['export_map'] = route_map_name

    def get_bgp_export_route_map(self, node, neighbor):
        """
        Get the name of the export route map to the given neighbor
        :param node:
        :param neighbor:
        :return: route_map_name
        """
        neighbors = self.get_bgp_neighbors(node)
        self.assert_valid_neighbor(node, neighbor)
        route_map_name = neighbors[neighbor].get('export_map', None)
        if not route_map_name:
            return None
        assert route_map_name in self.get_route_maps(node), \
            "Route map is not defiend %s" % route_map_name
        return route_map_name

    def get_route_maps(self, node):
        """Return dict of the configured route maps for a given router"""
        assert self.is_router(node)
        maps = self._node[node].get('routemaps', None)
        #print('maps', maps)
        if not maps:
            self._node[node]['routemaps'] = {}
            maps = self._node[node]['routemaps']
        return maps

    def add_route_map(self, node, routemap):
        assert isinstance(routemap, RouteMap)
        routemaps = self.get_route_maps(node)
        routemaps[routemap.name] = routemap
        return routemap

    def get_ip_preflix_lists(self, node):
        """Return a dict of the configured IP prefix lists"""
        if 'ip_prefix_lists' not in self._node[node]:
            self._node[node]['ip_prefix_lists'] = {}
        return self._node[node]['ip_prefix_lists']

    def add_ip_prefix_list(self, node, prefix_list):
        """Add new ip prefix list (overrides existing one with the same name"""
        assert isinstance(prefix_list, IpPrefixList)
        lists = self.get_ip_preflix_lists(node)
        if not prefix_list.name:
            name = None
            while not name or name in lists:
                name = 'ip_list_{}_{}'.format(node, next(self._counter))
            prefix_list._name = name
        err = "Prefix list with '{}' is already defined at router {}".format(
            node, prefix_list.name)
        #assert prefix_list.name not in lists, err
        lists[prefix_list.name] = prefix_list

    def del_ip_prefix_list(self, node, prefix_list):
        name = getattr(prefix_list, 'name', prefix_list)
        del self.get_ip_preflix_lists(node)[name]

    def set_iface_names(self):
        """Assigns interface IDs (Fa0/0,  Fa0/1, etc..) for each edge"""
        for node in sorted(list(self.nodes())):
            iface_count = 0
            for src, dst in sorted(list(self.out_edges(node))):
                if self.get_edge_iface(src, dst):
                    continue
                if self.is_router(src) and self.is_router(dst):
                    if self.get_edge_iface(src, dst):
                        continue
                    iface = "Fa%d/%d" % (iface_count // 2, iface_count % 2)
                    while iface in self.get_ifaces(src):
                        iface_count += 1
                        iface = "Fa%d/%d" % (iface_count // 2, iface_count % 2)
                    self.add_iface(src, iface, is_shutdown=False)
                    self.set_iface_addr(src, iface, VALUENOTSET)
                    self.set_edge_iface(src, dst, iface)
                    self.set_iface_description(src, iface, ''"To {}"''.format(dst))
                elif self.is_router(src) and self.is_network(dst):
                    iface = '{node}-veth{iface}'.format(node=src, iface=iface_count)
                    self.add_iface(src, iface, is_shutdown=False)
                    self.set_edge_iface(src, dst, iface)
                    self.set_iface_description(src, iface, ''"To {}"''.format(dst))
                elif self.is_network(src) and self.is_router(dst):
                    continue
                else:
                    raise ValueError('Not valid link %s -> %s' % (src, dst))

    def get_print_graph(self):
        """
        Get a plain version of the Network graph
        Mainly used to help visualizing it
        :return: networkx.DiGraph
        """
        graph = nx.DiGraph()
        for node, attrs in self.nodes(data=True):
            graph.add_node(node)
            vtype = str(attrs[VERTEX_TYPE])
            label = "%s\\n%s" % (node, vtype.split('.')[-1])
            asnum = self.get_bgp_asnum(node)
            if asnum:
                graph.node[node]['bgp_asnum'] = asnum
                label += "\nAS Num: %s" % asnum
            graph.node[node]['label'] = label
            graph.node[node][VERTEX_TYPE] = vtype

        for src, dst, attrs in self.edges(data=True):
            etype = str(attrs[EDGE_TYPE])
            graph.add_edge(src, dst)
            graph[src][dst]['label'] = etype.split('.')[-1]
            graph[src][dst][EDGE_TYPE] = etype

        return graph

    def get_simplified_graph(self):
        """
        A slightly more information rich representaiton of the graph
        than the print graph. This representation also contains the interfaces
        :return: networkx.DiGraph
        """
        graph = self.get_print_graph()
        for node, attrs in self.nodes(data=True):
            graph.node[node]['ifaces'] = self.get_ifaces(node)
            graph.node[node]['dyn'] = self.node[node]['dyn']
            graph.node[node]['loopbacks'] = self.node[node]['loopbacks']
        for src, dst, attrs in self.edges(data=True):
            graph[src][dst]['iface'] = self.get_edge_iface(src, dst)
        return graph

    def write_dot(self, out_file):
        """Write .dot file"""
        from networkx.drawing.nx_agraph import write_dot
        write_dot(self.get_print_graph(), out_file)

    def write_graphml(self, out_file):
        """Write graphml file"""
        nx.write_graphml(self.get_print_graph(), out_file)

    def write_propane(self, out_file):
        """Output propane style topology file"""
        out = '<topology asn="%d">\n' % 5
        for node in sorted(self.routers_iter()):
            internal = 'true' if self.is_local_router(node) else 'false'
            if self.is_bgp_enabled(node):
                asnum = self.get_bgp_asnum(node)
                out += '  <node internal="%s" asn="%d" name="%s"></node>\n' % (internal, asnum, node)
            else:
                out += '  <node internal="%s" name="%s"></node>\n' % (internal, node)
        seen = []
        for src, dst in self.edges():
            if (src, dst) in seen:
                continue
            out += '  <edge source="%s" target="%s"></edge>\n' % (src, dst)
            seen.append((src, dst))
            seen.append((dst, src))
        out += '</topology>\n'
        with open(out_file, 'w') as fd:
            fd.write(out)
