import argparse
import random
import sys
import json
import itertools
import time

from collections import defaultdict
from functools import partial
from timeit import default_timer as timer

import networkx as nx
from networkx.drawing.nx_agraph import write_dot
import xmltodict
import z3
import os
from ipaddress import ip_address, ip_network

from numpy.compat import unicode

from synet.synthesis.connected import ConnectedSyn
from synet.synthesis.new_propagation import EBGPPropagation
from tekton.bgp import Access
from tekton.bgp import ActionSetCommunity
from tekton.bgp import ActionSetLocalPref
from tekton.bgp import Announcement
from tekton.bgp import BGP_ATTRS_ORIGIN
from tekton.bgp import Community
from tekton.bgp import CommunityList
from tekton.bgp import MatchCommunitiesList
from tekton.bgp import RouteMap
from tekton.bgp import RouteMapLine
from tekton.bgp import IpPrefixList
from tekton.bgp import MatchIpPrefixListList
from tekton.bgp import MatchNextHop
from tekton.bgp import MatchSelectOne
from tekton.graph import NetworkGraph
from synet.utils.common import ECMPPathsReq
from synet.utils.common import KConnectedPathsReq
from synet.utils.common import PathOrderReq
from synet.utils.common import PathReq
from synet.utils.common import Protocols
from synet.utils.fnfree_smt_context import SolverContext
from synet.utils.fnfree_smt_context import VALUENOTSET
from synet.utils.fnfree_smt_context import is_empty
from synet.utils.fnfree_smt_context import read_announcements
from synet.utils.topo_gen import read_topology_from_json

from synet.utils.bgp_utils import compute_next_hop_map
from synet.utils.bgp_utils import extract_all_next_hops


def get_sym(concrete_anns, ctx):
    return read_announcements(concrete_anns, ctx)


def create_context(reqs, g, announcements, create_as_paths=False):
    connected = ConnectedSyn(reqs, g, full=True)
    connected.synthesize()
    next_hops_map = compute_next_hop_map(g)
    next_hops = extract_all_next_hops(next_hops_map)
    peers = [node for node in g.routers_iter() if g.is_bgp_enabled(node)]
    ctx = SolverContext.create_context(announcements, peer_list=peers,
                                       next_hop_list=next_hops, create_as_paths=create_as_paths)
    return ctx


def generate_policy(topo, custs, providers, peers):
    out = ''
    out += "define Peer = {%s}\n" % ', '.join(peers)
    out += "define Provider = {%s}\n" % ', '.join(providers)
    out += "define Cust = {%s}\n" % ', '.join(custs)
    out += "\n"
    out += "define NonCust = Peer + Provider\n"
    out += "\n"
    out += "define transit(X,Y) = enter(X+Y) & exit(X+Y)\n"
    out += "\n"
    out += "define notransit = {\n"
    out += "  true => not transit(NonCust, NonCust)\n"
    out += "}\n"
    out += "\n"
    out += "define routing = {\n"
    # for index, node in enumerate(sorted(list(topo.local_routers_iter()))):
    for index, node in enumerate(list(topo.local_routers_iter())):
        out += "  129.1.%d.0/24 => end(%s),\n" % (index + 1, node)
    out += "  true => exit(Cust >> Peer >> Provider)\n"
    out += "}\n"
    out += "define main = routing & notransit\n"
    return out


def read_propane(file):
    doc = {}
    topo = NetworkGraph()
    with open(file) as fd:
        doc = xmltodict.parse(fd.read())
        doc = doc['topology']

    for node in doc['node']:
        internal = node['@internal']
        asn = node['@asn']
        name = node['@name']
        topo.add_router(name)

    for edge in doc['edge']:
        source = edge['@source']
        target = edge['@target']
        topo.add_router_edge(source, target)
        topo.add_router_edge(target, source)
    topo.write_dot("/tmp/p.dot")


def setup_logging():
    import logging
    # create logger
    logger = logging.getLogger('synet')
    logger.setLevel(logging.DEBUG)

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    # create formatter
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # add formatter to ch
    ch.setFormatter(formatter)

    # add ch to logger
    logger.addHandler(ch)


def assign_ebgp(topo):
    """
    Assigns BGP AS numbers for nodes in the topology and establishes
    BGP sessions between every router and it's neighbors
    :param topo: NetworkGraph
    :return: None
    """
    assert isinstance(topo, NetworkGraph)
    # Assigning eBGP
    asnum_gen = itertools.count(10, step=10)
    for node in sorted(topo.local_routers_iter()):
        topo.set_bgp_asnum(node, asnum_gen.__next__())
    for src, dst in topo.edges():
        if not topo.is_router(src) or not topo.is_router(dst):
            continue
        if dst not in topo.get_bgp_neighbors(src):
            topo.add_bgp_neighbor(src, dst, VALUENOTSET, VALUENOTSET)


def set_access(line, access):
    """
    Auxiliary to set permit attribute in a RouteMapLine

    :param line: RoutMapLine
    :param access: Access.permit, Access.deny or VALUENOTSET
    :return: RouteMapLine
    """
    line.access = access
    return line


def set_comms(match, comms):
    """
    Auxiliary to set communities in Match
    :param match: Match
    :param comms: list of communities or VALUENOTSET
    :return: match
    """
    match.match.communities = comms
    return match


def syn_pref(set_pref_action, pref):
    """
    Auxiliary to set local pref value on SetLocalPrefAction
    :param set_pref_action: SetLocalPrefAction
    :param pref: int or VALUENOTSET
    :return: SetLocalPrefAction
    """
    set_pref_action.value = pref
    return set_pref_action


def setup_bgp(topo, ospf_reqs, all_communities):
    """
    Setup BGP experiment
    :param topo: NetworkGraph
    :param ospf_reqs: list of OSPF requirements to be converted to BGP
    :param all_communities: list of all known communities
    :return:
        all_reqs: same ospf_reqs but transformed for BGP reqs
        syn_vals: partially evaluated symbolic values to be saved
    """
    assert isinstance(topo, NetworkGraph)
    assign_ebgp(topo)
    all_reqs = []
    syn_vals = []
    # Generate AS Numbers for external peers
    peers_gen = itertools.count(10000, 10)
    peer_asnum = peers_gen.__next__()
    # for index, req in enumerate(sorted(ospf_reqs, key=lambda x: x.dst_net)):
    for index, req in enumerate(ospf_reqs):
        # Add node to the graph
        #print('index', index, req)
        egress = req.paths[0].path[-1]
        peer = "Peer%s" % (egress)
        topo.add_peer(peer)
        #print('peer', peer)
        # Set BGP properties for the new node
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        topo.add_peer_edge(peer, egress)
        topo.add_peer_edge(egress, peer)
        #topo.add_bgp_neighbor(peer, egress, VALUENOTSET, VALUENOTSET)
        # Tag by default announcements from the external peer
        peer_comm = all_communities[index]
        #print('peer_comm', peer_comm)
        set_comm = ActionSetCommunity([peer_comm])
        set_pref = ActionSetLocalPref(VALUENOTSET)
        line = RouteMapLine(matches=[], actions=[set_comm, set_pref], access=VALUENOTSET, lineno=10)
        syn_vals.append(partial(set_access, line=line, access=Access.permit))
        rname = "RMap_%s_from_%s" % (egress, peer)
        rmap = RouteMap(rname, lines=[line])
        topo.add_route_map(egress, rmap)
        topo.add_bgp_import_route_map(egress, peer, rname)
        #print('!!! add extrnal rname', rname)
        # Inject the initial announcement for that traffic class
        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        # The virtual peer inject the announcement to the network
        topo.add_bgp_advertise(peer, ann)
        # Extend the path requirements to include the virtual peer
        bgp_req = PathOrderReq(Protocols.BGP, prefix,
                               [PathReq(Protocols.BGP, prefix, tmp.path + [peer], False) for tmp in req.paths], False)
        all_reqs.append(bgp_req)
    return all_reqs, syn_vals


def gen_simple(topo, ospf_reqs, all_communities):
    assign_ebgp(topo)
    peer_asnum = 10000
    all_reqs = []
    syn_vals = []
    comm_lists = {}
    for router in topo.routers_iter():
        comm_lists[router] = itertools.count(1)

    # for index, req in enumerate(sorted(ospf_reqs, key=lambda x: x.dst_net)):
    for index, req in enumerate(ospf_reqs):
        # print 'X' * 50
        # print "REQ PATH", req
        # print 'X' * 50
        egress = req.path[-1]
        peer = "Peer%s_%d" % (egress, index)
        topo.add_peer(peer)
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        topo.add_peer_edge(peer, egress)
        topo.add_peer_edge(egress, peer)
        topo.add_bgp_neighbor(peer, egress, VALUENOTSET, VALUENOTSET)

        peer_comm = all_communities[index]
        set_comm = ActionSetCommunity([peer_comm])
        line = RouteMapLine(matches=None, actions=[set_comm], access=VALUENOTSET, lineno=10)
        syn_vals.append(partial(set_access, line=line, access=Access.permit))
        rname = "RMap_%s_from_%s" % (egress, peer)
        rmap = RouteMap(rname, lines=[line])
        topo.add_route_map(egress, rmap)
        topo.add_bgp_import_route_map(egress, peer, rname)

        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        bgp_req = PathReq(Protocols.BGP, prefix, req.path + [peer], False)
        all_reqs.append(bgp_req)
        for node in req.path:
            for _, neighbor in topo.out_edges(node):
                if neighbor in req.path:
                    continue
                clist = CommunityList(comm_lists[node].next(), Access.permit, [VALUENOTSET])
                match = MatchCommunitiesList(clist)
                syn_vals.append(partial(set_comms, match=match, comms=[peer_comm]))
                line = RouteMapLine(matches=[match], actions=[], access=VALUENOTSET, lineno=10)
                syn_vals.append(partial(set_access, line=line, access=Access.deny))
                rname = "RMap_%s_from_%s" % (neighbor, node)
                rmap = RouteMap(rname, lines=[line])
                topo.add_route_map(neighbor, rmap)
                topo.add_bgp_import_route_map(neighbor, node, rname)
    return all_reqs, syn_vals


def gen_order(topo, ospf_reqs, all_communities):
    assign_ebgp(topo)
    peer_asnum = 10000
    all_reqs = []

    syn_vals = []
    deny_map = {}
    pref_map = {}
    route_map_lines = {}
    export_route_maps = {}
    comm_subg = {}
    # for index, req in enumerate(sorted(ospf_reqs, key=lambda x: x.dst_net)):
    for index, req in enumerate(ospf_reqs):
        subg = nx.DiGraph()
        egress = req.paths[0].path[-1]
        peer = "Peer%s" % (egress)
        topo.add_peer(peer)
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        topo.add_peer_edge(peer, egress)
        topo.add_peer_edge(egress, peer)
        topo.add_bgp_neighbor(peer, egress, VALUENOTSET, VALUENOTSET)

        peer_comm = all_communities[index]
        comm_subg[peer_comm] = nx.DiGraph()
        subg.add_edge(egress, peer, rank=0)
        comm_subg[peer_comm].add_edge(egress, peer, rank=0)
        set_comm = ActionSetCommunity([peer_comm])
        set_pref = ActionSetLocalPref(VALUENOTSET)
        #syn_vals.append(partial(syn_pref, set_pref, VALUENOTSET))
        line = RouteMapLine(matches=[], actions=[set_comm, set_pref], access=VALUENOTSET, lineno=10)
        syn_vals.append(partial(set_access, line=line, access=Access.permit))
        if egress not in route_map_lines:
            route_map_lines[egress] = {}
        if peer not in route_map_lines[egress]:
            route_map_lines[egress][peer] = []
        route_map_lines[egress][peer] = [line] + route_map_lines[egress][peer]
        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        sub = []
        covered_nodes = [peer]
        for rank, path in enumerate(req.paths):
            covered_nodes.extend(path.path)
            for src, dst in zip(path.path[0::1], path.path[1::1]):
                subg.add_edge(src, dst, rank=rank)
                comm_subg[peer_comm].add_edge(src, dst, rank=rank)
            sub.append(PathReq(Protocols.BGP, prefix, path.path + [peer], False))
        bgp_req = PathOrderReq(Protocols.BGP, prefix, sub, False)
        all_reqs.append(bgp_req)
        write_dot(subg, '/tmp/subg.dot')

    for node in topo:
        for from_node, _ in topo.in_edges_iter(node):
            if topo.is_peer(node):
                continue
            lineno = 10
            lines = []
            for comm in all_communities:
                if comm_subg[comm].has_node(node) and comm_subg[comm].out_degree(node) > 1:
                    clist = CommunityList("t_%s_import_%s_%s" % (node, from_node, comm), Access.permit, [VALUENOTSET])
                    match = MatchCommunitiesList(clist)
                    syn_vals.append(partial(set_comms, match=match, comms=[peer_comm]))
                    set_pref = ActionSetLocalPref(VALUENOTSET)
                    if comm_subg[comm].has_edge(node, from_node):
                        syn_vals.append(partial(syn_pref, set_pref, 200 - comm_subg[comm][node][from_node]['rank']))
                    else:
                        syn_vals.append(partial(syn_pref, set_pref, 100))
                    line = RouteMapLine(matches=[match], actions=[set_pref], access=VALUENOTSET, lineno=lineno)
                    lineno += 10
                    syn_vals.append(partial(set_access, line=line, access=Access.permit))
                    lines.append(line)
                if node not in route_map_lines:
                    route_map_lines[node] = {}
                if from_node not in route_map_lines[node]:
                    route_map_lines[node][from_node] = []
                route_map_lines[node][from_node].extend(lines)

        for _, to_node in topo.out_edges_iter(node):
            if topo.is_peer(node):
                continue
            lineno = 10
            lines = []
            for comm in all_communities:
                if not comm_subg[comm].has_node(node):
                    continue
                clist = CommunityList("t_%s_export_%s_%s" % (node, to_node, comm), Access.permit, [VALUENOTSET])
                match = MatchCommunitiesList(clist)
                syn_vals.append(partial(set_comms, match=match, comms=[comm]))
                line = RouteMapLine(matches=[match], actions=[], access=VALUENOTSET, lineno=lineno)
                lineno += 10
                if comm_subg[comm].has_edge(to_node, node):
                    syn_vals.append(partial(set_access, line=line, access=Access.permit))
                else:
                    syn_vals.append(partial(set_access, line=line, access=Access.deny))
                lines.append(line)
            if node not in export_route_maps:
                export_route_maps[node] = {}
            if from_node not in export_route_maps[node]:
                export_route_maps[node][to_node] = []
                export_route_maps[node][to_node].extend(lines)

    for node in route_map_lines:
        for from_node, lines in route_map_lines[node].iteritems():
            if not lines:
                continue
            rname = "RMap_%s_from_%s" % (node, from_node)
            #if rname == 'RMap_Lille_from_London':
            #    assert False, lines
            rmap = RouteMap(rname, lines=lines)
            topo.add_route_map(node, rmap)
            topo.add_bgp_import_route_map(node, from_node, rname)
    for node in export_route_maps:
        for to_node, lines in export_route_maps[node].iteritems():
            if not lines:
                continue
            rname = "RMap_%s_to_%s" % (node, to_node)
            rmap = RouteMap(rname, lines=lines)
            topo.add_route_map(node, rmap)
            topo.add_bgp_export_route_map(node, to_node, rname)
    return all_reqs, syn_vals


def gen_kconnected(topo, ospf_reqs, all_communities):
    assign_ebgp(topo)
    peer_asnum = 10000
    all_reqs = []
    subg = nx.DiGraph()
    for index, req in enumerate(ospf_reqs):
        egress = req.paths[0].path[-1]
        peer = "Peer%s_%d" % (egress, index)
        topo.add_peer(peer)
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        topo.add_peer_edge(peer, egress)
        topo.add_peer_edge(egress, peer)
        topo.add_bgp_neighbor(peer, egress, VALUENOTSET, VALUENOTSET)

        set_comm = ActionSetCommunity([all_communities[index]])
        line = RouteMapLine(matches=[], actions=[set_comm], access=VALUENOTSET, lineno=10)
        rname = "RMap_%s_from_%s" % (egress, peer)
        rmap = RouteMap(rname, lines=[line])
        topo.add_route_map(egress, rmap)
        topo.add_bgp_import_route_map(egress, peer, rname)

        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        sub = []
        covered_nodes = []
        for path in req.paths:
            covered_nodes.extend(path.path)
            for src, dst in zip(path.path[0::1], path.path[1::1]):
                subg.add_edge(src, dst)
            sub.append(PathReq(Protocols.BGP, prefix, path.path + [peer], False))
        bgp_req = KConnectedPathsReq(Protocols.BGP, prefix, sub, False)
        all_reqs.append(bgp_req)
        for node in covered_nodes:
            for _, neighbor in topo.out_edges(node):
                if neighbor in covered_nodes:
                    continue
                clist = CommunityList("t", Access.permit, [VALUENOTSET])
                match = MatchCommunitiesList(clist)
                line = RouteMapLine(matches=[match], actions=[], access=VALUENOTSET, lineno=10)
                rname = "RMap_%s_from_%s" % (neighbor, node)
                rmap = RouteMap(rname, lines=[line])
                topo.add_route_map(neighbor, rmap)
                topo.add_bgp_import_route_map(neighbor, node, rname)
        for node in subg.nodes():
            if subg.out_degree(node) > 1:
                for _, neighbor in subg.out_edges(node):
                    clist = CommunityList("t", Access.permit, [VALUENOTSET])
                    match = MatchCommunitiesList(clist)
                    set_pref = ActionSetLocalPref(VALUENOTSET)
                    line = RouteMapLine(matches=[match], actions=[set_pref], access=VALUENOTSET, lineno=10)
                    rname = "RMap_%s_from_%s" % (neighbor, node)
                    #print "ADD IMPORT", rname
                    rmap = RouteMap(rname, lines=[line])
                    topo.add_route_map(neighbor, rmap)
                    topo.add_bgp_import_route_map(neighbor, node, rname)
    return all_reqs


def gen_ecmp2(topo, ospf_reqs, all_communities):
    # Assigning iBGP
    asnum = 10
    for node in sorted(topo.local_routers_iter()):
        topo.set_bgp_asnum(node, asnum)
    peer_asnum = 10000
    all_reqs = []

    for index, req in enumerate(ospf_reqs):
        subg = nx.DiGraph()
        egress = req.paths[0].path[-1]
        peer = "Peer%s_%d" % (egress, index)
        subg.add_edge(egress, peer)
        topo.add_peer(peer)
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        topo.add_peer_edge(peer, egress)
        topo.add_peer_edge(egress, peer)
        for lnode in topo.local_routers_iter():
            topo.add_bgp_neighbor(peer, lnode, VALUENOTSET, VALUENOTSET)

        set_comm = ActionSetCommunity([all_communities[index]])
        line = RouteMapLine(matches=[], actions=[set_comm], access=VALUENOTSET, lineno=10)
        rname = "RMap_%s_from_%s" % (egress, peer)
        rmap = RouteMap(rname, lines=[line])
        topo.add_route_map(egress, rmap)
        topo.add_bgp_import_route_map(egress, peer, rname)

        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        sub = []
        covered_nodes = []
        for path in req.paths:
            covered_nodes.extend(path.path)
            for src, dst in zip(path.path[0::1], path.path[1::1]):
                subg.add_edge(src, dst)
            sub.append(PathReq(Protocols.BGP, prefix, path.path + [peer], False))
        bgp_req = ECMPPathsReq(Protocols.BGP, prefix, sub, False)
        all_reqs.append(bgp_req)
        source = bgp_req.paths[0].path[0]
        for node in subg.nodes():
            if node in [source, peer]:
                continue
            all_paths = list(nx.all_shortest_paths(subg, node, peer))
            bgp_req2 = ECMPPathsReq(Protocols.BGP, prefix, [
                PathReq(Protocols.BGP, prefix, path, False) for path in all_paths
            ], False)
            all_reqs.append(bgp_req2)
        #for node in covered_nodes:
        #    for _, neighbor in topo.out_edges(node):
        #        if neighbor in covered_nodes:
        #            continue
        #        clist = CommunityList("t", Access.permit, [VALUENOTSET])
        #        match = MatchCommunitiesList(clist)
        #        line = RouteMapLine(matches=[match], actions=[], access=VALUENOTSET, lineno=10)
        #        rname = "RMap_%s_from_%s" % (neighbor, node)
        #        rmap = RouteMap(rname, lines=[line])
        #        topo.add_route_map(neighbor, rmap)
        #        topo.add_bgp_import_route_map(neighbor, node, rname)
        #for node in subg.nodes():
        #    if subg.out_degree(node) > 1:
        #        for _, neighbor in subg.out_edges(node):
        #            clist = CommunityList("t", Access.permit, [VALUENOTSET])
        #            match = MatchCommunitiesList(clist)
        #            set_pref = ActionSetLocalPref(VALUENOTSET)
        #            line = RouteMapLine(matches=[match], actions=[set_pref], access=VALUENOTSET, lineno=10)
        #            rname = "RMap_%s_from_%s" % (neighbor, node)
        #            print "ADD IMPORT", rname
        #            rmap = RouteMap(rname, lines=[line])
        #            topo.add_route_map(neighbor, rmap)
        #            topo.add_bgp_import_route_map(neighbor, node, rname)
    return all_reqs



def gen_ecmp(topo, ospf_reqs, all_communities):
    assign_ebgp(topo)
    peer_asnum = 10000
    all_reqs = []

    syn_vals = []
    deny_map = {}
    pref_map = {}
    route_map_lines = {}
    export_route_maps = {}
    comm_subg = {}
    for index, req in enumerate(ospf_reqs):
        subg = nx.DiGraph()
        egress = req.paths[0].path[-1]
        peer = "Peer%s_%d" % (egress, index)
        topo.add_peer(peer)
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        topo.add_peer_edge(peer, egress)
        topo.add_peer_edge(egress, peer)
        topo.add_bgp_neighbor(peer, egress, VALUENOTSET, VALUENOTSET)

        peer_comm = all_communities[index]
        comm_subg[peer_comm] = nx.DiGraph()
        subg.add_edge(egress, peer, rank=0)
        comm_subg[peer_comm].add_edge(egress, peer, rank=0)
        set_comm = ActionSetCommunity([peer_comm])
        set_pref = ActionSetLocalPref(VALUENOTSET)
        #syn_vals.append(partial(syn_pref, set_pref, VALUENOTSET))
        line = RouteMapLine(matches=[], actions=[set_comm, set_pref], access=VALUENOTSET, lineno=10)
        syn_vals.append(partial(set_access, line=line, access=Access.permit))
        if egress not in route_map_lines:
            route_map_lines[egress] = {}
        if peer not in route_map_lines[egress]:
            route_map_lines[egress][peer] = []
        route_map_lines[egress][peer] = [line] + route_map_lines[egress][peer]
        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        sub = []
        covered_nodes = [peer]
        for rank, path in enumerate(req.paths):
            covered_nodes.extend(path.path)
            for src, dst in zip(path.path[0::1], path.path[1::1]):
                subg.add_edge(src, dst, rank=rank)
                comm_subg[peer_comm].add_edge(src, dst, rank=rank)
            sub.append(PathReq(Protocols.BGP, prefix, path.path + [peer], False))
        bgp_req = PathOrderReq(Protocols.BGP, prefix, sub, False)
        all_reqs.append(bgp_req)
        write_dot(subg, '/tmp/subg.dot')

    for node in topo:
        for from_node, _ in topo.in_edges(node):
            if topo.is_peer(node):
                continue
            lineno = 10
            lines = []
            for comm in all_communities:
                if comm_subg[comm].has_node(node) and comm_subg[comm].out_degree(node) > 1:
                    clist = CommunityList("t_%s_import_%s_%s" % (node, from_node, comm), Access.permit, [VALUENOTSET])
                    match = MatchCommunitiesList(clist)
                    syn_vals.append(partial(set_comms, match=match, comms=[peer_comm]))
                    set_pref = ActionSetLocalPref(VALUENOTSET)
                    if comm_subg[comm].has_edge(node, from_node):
                        syn_vals.append(partial(syn_pref, set_pref, 200))
                    else:
                        syn_vals.append(partial(syn_pref, set_pref, 100))
                    line = RouteMapLine(matches=[match], actions=[set_pref], access=VALUENOTSET, lineno=lineno)
                    lineno += 10
                    syn_vals.append(partial(set_access, line=line, access=Access.permit))
                    lines.append(line)
                if node not in route_map_lines:
                    route_map_lines[node] = {}
                if from_node not in route_map_lines[node]:
                    route_map_lines[node][from_node] = []
                route_map_lines[node][from_node].extend(lines)

        for _, to_node in topo.out_edges(node):
            if topo.is_peer(node):
                continue
            lineno = 10
            lines = []
            for comm in all_communities:
                if not comm_subg[comm].has_node(node):
                    continue
                clist = CommunityList("t_%s_export_%s_%s" % (node, to_node, comm), Access.permit, [VALUENOTSET])
                match = MatchCommunitiesList(clist)
                syn_vals.append(partial(set_comms, match=match, comms=[comm]))
                line = RouteMapLine(matches=[match], actions=[], access=VALUENOTSET, lineno=lineno)
                lineno += 10
                if comm_subg[comm].has_edge(to_node, node):
                    syn_vals.append(partial(set_access, line=line, access=Access.permit))
                else:
                    syn_vals.append(partial(set_access, line=line, access=Access.deny))
                lines.append(line)
            if node not in export_route_maps:
                export_route_maps[node] = {}
            if from_node not in export_route_maps[node]:
                export_route_maps[node][to_node] = []
                export_route_maps[node][to_node].extend(lines)

    for node in route_map_lines:
        for from_node, lines in route_map_lines[node].iteritems():
            if not lines:
                continue
            rname = "RMap_%s_from_%s" % (node, from_node)
            #if rname == 'RMap_Lille_from_London':
            #    assert False, lines
            rmap = RouteMap(rname, lines=lines)
            topo.add_route_map(node, rmap)
            topo.add_bgp_import_route_map(node, from_node, rname)
    for node in export_route_maps:
        for to_node, lines in export_route_maps[node].iteritems():
            if not lines:
                continue
            rname = "RMap_%s_to_%s" % (node, to_node)
            rmap = RouteMap(rname, lines=lines)
            topo.add_route_map(node, rmap)
            topo.add_bgp_export_route_map(node, to_node, rname)
    return all_reqs, syn_vals



def gen_simple_abs(topo, ospf_reqs, all_communities, partially_evaluated, inv_prefix_map):
    assert isinstance(topo, NetworkGraph)
    assign_ebgp(topo)
    peer_asnum = 10000
    all_reqs = []
    comm_lists = {}
    for router in topo.routers_iter():
        comm_lists[router] = itertools.count(1)

    # for index, req in enumerate(sorted(ospf_reqs, key=lambda x: x.dst_net)):
    for index, req in enumerate(ospf_reqs):
        egress = req.path[-1]
        peer = "Peer%s" % egress
        topo.add_peer(peer)
        peer_asnum += 10
        topo.set_bgp_asnum(peer, peer_asnum)
        # topo.add_peer_edge(peer, egress)
        # topo.add_peer_edge(egress, peer)
        #topo.add_bgp_neighbor(peer, egress, VALUENOTSET, VALUENOTSET)

        peer_comm = all_communities[index]
        set_comm = ActionSetCommunity([peer_comm])
        line = RouteMapLine(matches=None, actions=[set_comm], access=VALUENOTSET, lineno=10)
        rname = "RMap_%s_from_%s" % (egress, peer)
        rmap = RouteMap(rname, lines=[line])
        topo.add_route_map(egress, rmap)
        topo.add_bgp_import_route_map(egress, peer, rname)

        cs = dict([(c, False) for c in all_communities])
        prefix = 'P_%s' % (peer,)
        ann = Announcement(
            prefix=prefix,
            peer=peer,
            origin=BGP_ATTRS_ORIGIN.EBGP,
            as_path=[1, 2], as_path_len=2,
            next_hop='%sHop' % peer, local_pref=100, med=10,
            communities=cs, permitted=True)
        topo.add_bgp_advertise(peer, ann)
        bgp_req = PathReq(Protocols.BGP, prefix, req.path + [peer], False)
        all_reqs.append(bgp_req)
        for node in req.path:
            for _, neighbor in topo.out_edges(node):
                if neighbor in req.path:
                    export_rmap_name = "RMap_%s_to_%s" % (node, neighbor)
                    if export_rmap_name in partially_evaluated:
                        rmap_des = deserialize_route_map(topo, neighbor, export_rmap_name,
                                                         partially_evaluated[export_rmap_name], inv_prefix_map)
                        topo.add_route_map(node, rmap_des)
                        topo.add_bgp_export_route_map(node, neighbor, rmap_des.name)
                    continue
                rname = "RMap_%s_from_%s" % (neighbor, node)
                if rname in partially_evaluated:
                    rmap_des = deserialize_route_map(topo, neighbor, rname, partially_evaluated[rname], inv_prefix_map)
                    topo.add_route_map(neighbor, rmap_des)
                    topo.add_bgp_import_route_map(neighbor, node, rname)
                    next(comm_lists[node])
                    continue
    return all_reqs


def gen_order_abs(topo, ospf_reqs, all_communities, partially_evaluated, inv_prefix_map):
    assert isinstance(topo, NetworkGraph)
    all_reqs, syn_vals = setup_bgp(topo, ospf_reqs, all_communities)
    syn_vals = []

    # A generator for unique community list id per router
    comm_list_id_gen = {}
    for router in topo.routers_iter():
        comm_list_id_gen[router] = itertools.count(1)
    all_peers = []
    # Compute number of imports from a neighbor
    import_degree = defaultdict(lambda: defaultdict(lambda: 0))
    # for index, req in enumerate(sorted(all_reqs, key=lambda x: x.dst_net)):
    for index, req in enumerate(all_reqs):
        peer = req.paths[0].path[-1]
        all_peers.append(peer)
        for rank, path in enumerate(req.paths):
            for src, dst in zip(path.path[0::1], path.path[1::1]):
                import_degree[src][dst] += 1

    for node in sorted(topo.nodes()):
        if topo.is_peer(node):
            continue
        for neighbor in sorted(topo.neighbors(node)):

            export_rmap_name = "RMap_%s_to_%s" % (node, neighbor)
            if export_rmap_name in partially_evaluated:
                rmap_des = deserialize_route_map(topo, neighbor, export_rmap_name,
                                                 partially_evaluated[export_rmap_name], inv_prefix_map)
                topo.add_route_map(node, rmap_des)
                topo.add_bgp_export_route_map(node, neighbor, rmap_des.name)
            # else:
            #     line = RouteMapLine(matches=None, actions=None, access=VALUENOTSET, lineno=100)
            #     export_rmap = RouteMap(export_rmap_name, lines=[line])
            #     topo.add_route_map(node, export_rmap)
            #     topo.add_bgp_export_route_map(node, neighbor, export_rmap_name)

            if topo.is_peer(neighbor):
                continue

            import_rmap_name = "RMap_%s_from_%s" % (node, neighbor)
            if import_rmap_name in partially_evaluated:
                rmap_des = deserialize_route_map(topo, neighbor, import_rmap_name,
                                                 partially_evaluated[import_rmap_name], inv_prefix_map)
                topo.add_route_map(node, rmap_des)
                topo.add_bgp_import_route_map(node, neighbor, import_rmap_name)
            # else:
            #     lines = []
            #     lineno_gen = itertools.count(10, step=10)
            #     for i in range(import_degree[node][neighbor]):
            #         clist = CommunityList(comm_list_id_gen[node].__next__(), Access.permit,
            #                               [VALUENOTSET, VALUENOTSET, VALUENOTSET])
            #         topo.add_bgp_community_list(node, clist)
            #         match_comms = MatchCommunitiesList(clist)
            #         ip_list = IpPrefixList(name='IpL_%s_%s_%d' % (neighbor, node, i), access=Access.permit,
            #                                networks=[VALUENOTSET])
            #         topo.add_ip_prefix_list(neighbor, ip_list)
            #         match_ip = MatchIpPrefixListList(ip_list)
            #         match_next_hop = MatchNextHop(VALUENOTSET)
            #         #match = MatchSelectOne([match_comms, match_ip, match_next_hop])
            #         match = MatchSelectOne([match_comms])
            #         line = RouteMapLine(matches=[match],
            #                             actions=[ActionSetLocalPref(VALUENOTSET), ActionSetCommunity([VALUENOTSET])],
            #                             access=VALUENOTSET, lineno=lineno_gen.__next__())
            #         lines.append(line)
            #     line_deny = RouteMapLine(matches=None, actions=None, access=Access.deny, lineno=lineno_gen.__next__())
            #     lines.append(line_deny)
            #     import_rmap_name = "RMap_%s_from_%s" % (node, neighbor)
            #     rmap = RouteMap(import_rmap_name, lines=lines)
            #     topo.add_route_map(node, rmap)
            #     topo.add_bgp_import_route_map(node, neighbor, import_rmap_name)
    return all_reqs, syn_vals



def deserialize_route_map(topo, node, name, rmap, inv_prefix_map):
    lines = []
    for line in rmap:
        lines.append(deserialize_route_map_line(topo, node, line, inv_prefix_map))
    return RouteMap(name=name, lines=lines)


def deserialize_route_map_line(topo, node, line, inv_prefix_map):
    matches = deserialize_matches(topo, node, line['matches'], inv_prefix_map)
    access = deserialize_acces(line['access'])
    lineno = line['lineno']
    actions = deserialize_actions(topo, node, line['actions'])
    return RouteMapLine(matches=matches, actions=actions, access=access, lineno=lineno)


def deserialize_actions(topo, node, actions):
    if not actions:
        return None
    ret = []
    for action in actions[:]:
        if action['action'] == 'ActionSetLocalPref':
            ret.append(ActionSetLocalPref(action['value']))
        elif action['action'] == 'ActionSetCommunity':
            comms = [Community(c) if not is_empty(c) else c for c in action['communities']]
            additive = action['additive']
            ret.append(ActionSetCommunity(communities=comms, additive=additive))
    return ret


def deserialize_acces(access):
    if is_empty(access):
        return VALUENOTSET
    assert access in [u'permit', u'deny', VALUENOTSET], access

    return Access.permit if access == u'permit' else Access.deny


def deserialize_iplist(iplist, inv_prefix_map):
    networks = [inv_prefix_map.get(net, net) for net in iplist['networks']]
    access = deserialize_acces(iplist['access'])
    name = iplist['name']
    return IpPrefixList(name=name, access=access, networks=networks)


def deserialize_comm_list(topo, node, commslist):
    #print "\tD", commslist
    communities = [Community(str(comm)) if not is_empty(comm) else comm for comm in commslist['communities']]
    access = deserialize_acces(commslist['access'])
    list_id = commslist['list_id']
    assert not is_empty(access)
    comm_list = CommunityList(list_id=list_id, access=access, communities=communities)
    if comm_list.list_id in topo.get_bgp_communities_list(node):
        assert comm_list == topo.get_bgp_communities_list(node)[comm_list.list_id]
    else:
        topo.add_bgp_community_list(node, comm_list)
    return comm_list


def deserialize_matches(topo, node, matches, inv_prefix_map):
    ret = []
    if not matches:
        return None
    for match in matches[:]:
        if match['match_type'] == 'MatchNextHop':
            next_hop = inv_prefix_map.get(match['nexthop'], match['nexthop'])
            ret.append(MatchNextHop(next_hop))
        elif match['match_type'] == 'MatchIpPrefixListList':
            iplist = deserialize_iplist(match['prefix_list'], inv_prefix_map)
            topo.add_ip_prefix_list(node, iplist)
            ret.append(MatchIpPrefixListList(iplist))
        elif match['match_type'] == 'MatchCommunitiesList':
            comms = deserialize_comm_list(topo, node, match['communities_list'])
            ret.append(MatchCommunitiesList(comms))
        else:
            raise NotImplementedError(match)
    return ret


def serialize_action(action, prefix_map):
    if isinstance(action, ActionSetLocalPref):
        if is_empty(action.value):
            return
        return {'action': 'ActionSetLocalPref', 'value': action.value}
    elif isinstance(action, ActionSetCommunity):
        comms = [c.value for c in action.communities if not is_empty(c)]
        if not comms:
            return
        return {'action': 'ActionSetCommunity', 'communities': comms, 'additive': action.additive}
    else:
        raise NotImplementedError(action)


def ser_access(access):
    return 'permit' if access == Access.permit else 'deny'


def serialize_match(match, prefix_map):
    if isinstance(match, MatchNextHop):
        if is_empty(match.match):
            return
        nexthop = prefix_map.get(str(match.match), match.match)
        return {'match_type': 'MatchNextHop', 'nexthop': unicode(nexthop)}
    elif isinstance(match, MatchIpPrefixListList):
        ips = [unicode(prefix_map.get(c, c)) for c in match.match.networks if not is_empty(c)]
        if not ips:
            return
        tmp = {'match_type': 'MatchIpPrefixListList', 'prefix_list': {'name': match.match.name, 'access': ser_access(match.match.access), 'networks': ips}}
        return tmp
    elif isinstance(match, MatchCommunitiesList):
        comms = [c.value for c in match.match.communities if not is_empty(c)]
        if not comms:
            return
        return {'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': match.match.list_id, 'access': ser_access(match.match.access), 'communities': comms}}
    else:
        raise NotImplementedError(match)


def serialize_route_map(rmap, prefix_map):
    ret = []
    for line in rmap.lines:
        if is_empty(line.access):
            continue
        matches = [serialize_match(match, prefix_map) for match in line.matches]
        matches = [match for match in matches if match]
        matches = matches if matches else None

        actions = [serialize_action(action, prefix_map) for action in line.actions]
        actions = [action for action in actions if action]
        actions = actions if actions else None

        ret.append(
            {'name': rmap.name,
             'access': ser_access(line.access),
             'lineno': line.lineno,
             'matches': matches,
             'actions': actions})
    return ret


def make_symb_matches(matches):
    new_matches = []
    if not matches:
        return None
    for match in matches:
        if match['match_type'] == 'MatchNextHop':
            new_matches.append({'match_type': 'MatchNextHop', 'nexthop': VALUENOTSET})
        elif match['match_type'] == 'MatchIpPrefixListList':
            new_match = {
                'match_type': 'MatchIpPrefixListList',
                'prefix_list': {
                    'name': match['prefix_list'],
                    'access': 'permit',
                    'networks': [VALUENOTSET for _ in match['prefix_list']]}}
            new_matches.append(new_match)
        elif match['match_type'] == 'MatchCommunitiesList':
            new_match = {
                'match_type': 'MatchCommunitiesList',
                'communities_list': {
                    'list_id': match['communities_list']['list_id'],
                    'access': 'permit',
                    'communities': [VALUENOTSET for _ in match['communities_list']['communities']]}}
            new_matches.append(new_match)
        else:
            raise NotImplementedError(match)
    return new_matches

def make_symb_actions(actions):
    if not actions:
        return
    new_actions = []
    for action in actions:
        if action['action'] == 'ActionSetLocalPref':
            new_actions.append({'action': 'ActionSetLocalPref', 'value': VALUENOTSET})
            #print('!!!action', action)
            #new_actions.append({'action': 'ActionSetLocalPref', 'value': action['value']})
        elif action['action'] == 'ActionSetCommunity':
            new_action = {
                'action': 'ActionSetCommunity',
                'additive': action['communities'],
                'communities': [VALUENOTSET for _ in action['communities']]}
            new_actions.append(new_action)
    return new_actions


def make_symb_line(line):
    #print('!!!line', line)
    name = line['name']
    if '_from_' in name:
        new_line = {
            'name': line['name'],
            'access': line['access'],  # VALUENOTSET,
            'lineno': line['lineno'],
            'matches': make_symb_matches(line['matches']),
            'actions': make_symb_actions(line['actions']),
        }
    else:
        new_line = {
            'name': line['name'],
            'access': VALUENOTSET,
            'lineno': line['lineno'],
            'matches': make_symb_matches(line['matches']),
            'actions': make_symb_actions(line['actions']),
        }
    # new_line = {
    #     'name': line['name'],
    #     'access': line['access'], #VALUENOTSET,
    #     'lineno': line['lineno'],
    #     'matches': make_symb_matches(line['matches']),
    #     'actions': make_symb_actions(line['actions']),
    # }
    return new_line


def make_symbolic_attrs(rmap):
    new_lines = []
    for line in rmap:
        new_lines.append(make_symb_line(line))
    return new_lines

def autoGenerateBGPpolicy(intents):
    policies = []
    for intent in intents:
        des = intent[0][-1]
        print('intent', intent[0])
        policies.append(PathOrderReq(Protocols.BGP, des, [PathReq(Protocols.BGP, des, intent[0], False),
                                               PathReq(Protocols.BGP, des,
                                                       intent[1], False)], False))
    return policies



def Bgpeval(req_type, reqsize, reqs, template_file): #
    #print('!!!req_type', req_type)
    fixed = 1

    sketch_type = 'attrs'

    topo_file = './input/topology.json'
    #topo_file = 'Arnes.json'
    with open(topo_file, 'r') as json_file:
        topo_json_file = json.load(json_file)
        # print('topo_json_file', topo_json_file)

    topo = read_topology_from_json(topo_json_file)

    #topo = read_topology_zoo_netgraph(topo_file)
    #print('!!!nodes', topo.nodes)
    reqsize = reqsize
    #print('reqsize', reqsize)

    partially_eval_rmaps = {}
    inv_prefix_map = {}
    if fixed > 0:
        with open(template_file, 'r') as ff:
            read_maps = json.load(ff)
            #print('read_maps', read_maps)
        #read_maps = {'rmaps': {'RMap_ra_from_rb': [{'name': 'RMap_ra_from_rb', 'access': 'permit', 'lineno': 10, 'matches': [{'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': 1, 'access': 'permit', 'communities': ['EMPTY?Value']}}], 'actions': [{'action': 'ActionSetLocalPref', 'value': '100'}, {'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}]}, {'name': 'RMap_ra_from_rb', 'access': 'deny', 'lineno': 20, 'matches': [], 'actions': []}], 'RMap_ra_from_rc': [{'name': 'RMap_ra_from_rc', 'access': 'permit', 'lineno': 10, 'matches': [{'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': 1, 'access': 'permit', 'communities': ['EMPTY?Value']}}], 'actions': [{'action': 'ActionSetLocalPref', 'value': '200'}, {'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}]}, {'name': 'RMap_ra_from_rc', 'access': 'deny', 'lineno': 20, 'matches': [], 'actions': []}], 'RMap_rb_from_ra': [{'name': 'RMap_rb_from_ra', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_rb_from_rd': [{'name': 'RMap_rb_from_rd', 'access': 'permit', 'lineno': 10, 'matches': [{'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': 1, 'access': 'permit', 'communities': ['EMPTY?Value']}}], 'actions': [{'action': 'ActionSetLocalPref', 'value': '100'}, {'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}]}, {'name': 'RMap_rb_from_rd', 'access': 'deny', 'lineno': 20, 'matches': [], 'actions': []}], 'RMap_rc_from_ra': [{'name': 'RMap_rc_from_ra', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_rc_from_re': [{'name': 'RMap_rc_from_re', 'access': 'permit', 'lineno': 10, 'matches': [{'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': 1, 'access': 'permit', 'communities': ['EMPTY?Value']}}], 'actions': [{'action': 'ActionSetLocalPref', 'value': '100'}, {'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}]}, {'name': 'RMap_rc_from_re', 'access': 'deny', 'lineno': 20, 'matches': [], 'actions': []}], 'RMap_rd_from_rb': [{'name': 'RMap_rd_from_rb', 'access': 'permit', 'lineno': 10, 'matches': [{'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': 1, 'access': 'permit', 'communities': ['EMPTY?Value']}}], 'actions': [{'action': 'ActionSetLocalPref', 'value': '100'}, {'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}]}, {'name': 'RMap_rd_from_rb', 'access': 'deny', 'lineno': 20, 'matches': [], 'actions': []}], 'RMap_rd_from_re': [{'name': 'RMap_rd_from_re', 'access': 'permit', 'lineno': 10, 'matches': [{'match_type': 'MatchCommunitiesList', 'communities_list': {'list_id': 1, 'access': 'permit', 'communities': ['EMPTY?Value']}}], 'actions': [{'action': 'ActionSetLocalPref', 'value': '100'}, {'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}]}, {'name': 'RMap_rd_from_re', 'access': 'deny', 'lineno': 20, 'matches': [], 'actions': []}], 'RMap_rd_from_rf': [{'name': 'RMap_rd_from_rf', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_re_from_rc': [{'name': 'RMap_re_from_rc', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_re_from_rd': [{'name': 'RMap_re_from_rd', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_re_from_rg': [{'name': 'RMap_re_from_rg', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_External_re_from_Peerre': [{'name': 'RMap_External_re_from_Peerre', 'access': 'permit', 'lineno': 10, 'matches': [], 'actions': [{'action': 'ActionSetCommunity', 'communities': ['100:0'], 'additive': True}, {'action': 'ActionSetLocalPref', 'value': '100'}]}], 'RMap_re_to_Peerre': [{'name': 'RMap_re_to_Peerre', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_rf_from_rd': [{'name': 'RMap_rf_from_rd', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_rf_from_rg': [{'name': 'RMap_rf_from_rg', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_rg_from_rf': [{'name': 'RMap_rg_from_rf', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_rg_from_re': [{'name': 'RMap_rg_from_re', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}], 'RMap_Peerre_from_re': [{'name': 'RMap_Peerre_from_re', 'access': 'deny', 'lineno': 10, 'matches': [], 'actions': []}]}}
            # inv_prefix_map = read_maps['inv_prefix_map']
        # sampled_maps = rand.sample(read_maps['rmaps'].keys(), int(round(len(read_maps) * fixed)))
        sampled_maps = read_maps['rmaps'].keys()
        if sketch_type == 'abs':
            for name in sampled_maps:
                partially_eval_rmaps[name] = read_maps['rmaps'][name]
            #print('partially_eval_rmaps', partially_eval_rmaps)
        elif sketch_type == 'attrs':
            #print('sampled_maps', sampled_maps)
            import copy
            for name in read_maps['rmaps']:
                if name not in sampled_maps:
                    #print('!!!name', name)
                    partially_eval_rmaps[name] = copy.copy(read_maps['rmaps'][name])
                else:
                    # print('line', read_maps['rmaps'][name])
                    # print('make_symbolic_attrs', make_symbolic_attrs(read_maps['rmaps'][name]))
                    partially_eval_rmaps[name] = make_symbolic_attrs(copy.deepcopy(read_maps['rmaps'][name]))
                    # if name == "RMap_Celje_from_Velenj":
                    # # partially_eval_rmaps[name] = copy.copy(read_maps['rmaps'][name])
                    #     print('partially_eval_rmaps1', partially_eval_rmaps[name])
                    # print(read_maps['rmaps'][name])
        else:
            raise NotImplementedError(sketch_type)

    k = 2
    if req_type == 'simple':
        ospf_reqs = reqs
        all_communities = [Community("100:%d" % i) for i in range(len(ospf_reqs))]
        # all_reqs, syn_vals = gen_simple(topo, ospf_reqs, all_communities)
        all_reqs = gen_simple_abs(topo, ospf_reqs, all_communities, partially_eval_rmaps, inv_prefix_map)
    elif req_type == 'ecmp':
        ospf_reqs = reqs
        all_communities = [Community("100:%d" % i) for i in range(len(ospf_reqs))]
        all_reqs, syn_vals = gen_ecmp(topo, ospf_reqs, all_communities)
    elif req_type == 'kconnected':
        ospf_reqs = eval('reqs_kconnected_%d_%d' % (reqsize, k))
        raise NotImplementedError()
    elif req_type == 'order':
        ospf_reqs = reqs
        all_communities = [Community("100:%d" % i) for i in range(len(ospf_reqs))]
        all_reqs, syn_vals = gen_order_abs(topo, ospf_reqs, all_communities, partially_eval_rmaps, inv_prefix_map)
        #print('all_reqs', all_reqs, syn_vals)
        # all_reqs [PathOrderReq(Protocols.BGP, 'P_PeerAtlanta_0', [PathReq(Protocols.BGP, "P_PeerAtlanta_0", ['Houston', 'Atlanta', 'PeerAtlanta_0'], False), PathReq(Protocols.BGP, "P_PeerAtlanta_0", ['Houston', 'KansasCity', 'Indianapol', 'Atlanta', 'PeerAtlanta_0'], False)], False)] []
    else:
        raise ValueError("Unknow req type %s", req_type)

    conn = ConnectedSyn([], topo, full=True)
    conn.synthesize()

    announcements = []
    for peer in topo.peers_iter():
        #print('peer', peer)
        announcements.extend(topo.get_bgp_advertise(peer))
    #print('announcements', announcements)
    # prefixes = sorted([ann.prefix for ann in announcements])
    prefixes = [ann.prefix for ann in announcements]
    #print('prefixes', prefixes)
    ctx = create_context(all_reqs, topo, announcements)
    #print('!!!ctx', ctx)

    begin = timer()
    t1 = timer()
    # p = EBGPPropagation(all_reqs, topo, allow_igp=False)
    p = EBGPPropagation(all_reqs, topo, ctx)
    p.compute_dags()
    t2 = timer()
    prep = t2 - t1
    t1 = timer()
    p.synthesize()
    t2 = timer()
    bgp_syn = t2 - t1
    t1 = timer()
    solver = z3.Solver(ctx=ctx.z3_ctx)
    ret = ctx.check(solver)
    t2 = timer()
    z3_syn = t2 - t1
    if ret != z3.sat:
        return solver.unsat_core(), None
        #assert ret == z3.sat, solver.unsat_core()
    else:
        p.update_network_graph()
        from tekton.gns3 import GNS3Topo
        prefix_map = {}
        next_announced_prefix = int(ip_address(u'129.1.0.0'))
        for prefix in prefixes:
            ip = ip_address(next_announced_prefix)
            net = ip_network(u"%s/24" % ip)
            next_announced_prefix += 256
            prefix_map[prefix] = net
        gns3 = GNS3Topo(topo, prefix_map=prefix_map)
        # out_dir = 'out-configs/examples/%s' % (topo_name)
        # print('out_dir', out_dir)
        configs = gns3.write_configs()
        return None, configs
    #assert ret == z3.sat, solver.unsat_core()
    # p.update_network_graph()
    # p.update_network_graph()

#
# if __name__ == '__main__':
#     #req_type, reqs, topo_file, topo_name, destination
#     req_type = 'order'
#     topo_file = './topozoo/small/Arnes.json'
#     topo_name = 'Arnes'
#     destination = 'Velenj'
#     reqs = [[['Dravog', 'Sloven', 'Velenj'], ['Dravog', 'Maribo', 'Lasko', 'Celje', 'Velenj']],
#             [['Ljublj', 'Koper', 'Kranj'], ['Ljublj', 'NovaGo', 'Tolmin', 'Bled', 'Kranj']]]
#     template_file = 'routing_policy_templates.json'
#     specifications = autoGenerateBGPpolicy(reqs)
#     print('spec', specifications)
#     Bgpeval(req_type, 2, specifications, template_file)

