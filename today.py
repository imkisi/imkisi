import requests
import os
from lxml import etree
import time
import hashlib
import re

HEADERS = {'Authorization': 'Bearer ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def simple_request(func_name, query, variables):
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_repos_stars(count_type, owner_affiliation, cursor=None, total_stars=0):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        repo_data = request.json()['data']['user']['repositories']
        if count_type == 'repos':
            return repo_data['totalCount']
        elif count_type == 'stars':
            total_stars += stars_counter(repo_data['edges'])
            if repo_data['pageInfo']['hasNextPage']:
                return graph_repos_stars(
                    count_type,
                    owner_affiliation,
                    repo_data['pageInfo']['endCursor'],
                    total_stars
                )
            return total_stars


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        repo_obj = request.json().get('data', {}).get('repository')
        if repo_obj and repo_obj.get('defaultBranchRef') is not None:
            return loc_counter_one_repo(
                owner, repo_name, data, cache_comment,
                repo_obj['defaultBranchRef']['target']['history'],
                addition_total, deletion_total, my_commits
            )
        else:
            return 0, 0, 0
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time! You\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for edge in history.get('edges', []):
        if not edge or not edge.get('node'):
            continue
        author = edge['node'].get('author') or {}
        author_user = author.get('user')
        if author_user is not None and author_user == OWNER_ID:
            my_commits += 1
            addition_total += edge['node'].get('additions', 0)
            deletion_total += edge['node'].get('deletions', 0)

    if not history.get('edges') or not history.get('pageInfo', {}).get('hasNextPage'):
        return addition_total, deletion_total, my_commits
    else:
        return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef {
                                target {
                                    ... on Commit {
                                        history {
                                            totalCount
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repo_conn = request.json()['data']['user']['repositories']
    if repo_conn['pageInfo']['hasNextPage']:
        edges += repo_conn['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, repo_conn['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + repo_conn['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    edges = [e for e in edges if e and e.get('node')]
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    os.makedirs('cache', exist_ok=True)
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        node = edges[index]['node']
        if repo_hash == hashlib.sha256(node['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != node['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = node['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(node['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError:
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size]
    with open(filename, 'w') as f:
        f.writelines(data)
        for edge in edges:
            if edge and edge.get('node'):
                f.write(hashlib.sha256(edge['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)


def stars_counter(data):
    total_stars = 0
    for edge in data:
        if edge and edge.get('node') and edge['node'].get('stargazers'):
            total_stars += edge['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    with open(filename, 'r', encoding='utf-8') as f:
        xml_content = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', f.read())
    root = etree.fromstring(xml_content.encode('utf-8'))

    commit_str = f"{commit_data:,}" if isinstance(commit_data, int) else str(commit_data)
    star_str = f"{star_data:,}" if isinstance(star_data, int) else str(star_data)
    repo_str = f"{repo_data:,}" if isinstance(repo_data, int) else str(repo_data)
    contrib_str = f"{contrib_data:,}" if isinstance(contrib_data, int) else str(contrib_data)
    follower_str = f"{follower_data:,}" if isinstance(follower_data, int) else str(follower_data)
    loc_main_str = f"{loc_data[2]:,}" if isinstance(loc_data[2], int) else str(loc_data[2])
    loc_add_str = f"{loc_data[0]:,}" if isinstance(loc_data[0], int) else str(loc_data[0])
    loc_del_str = f"{loc_data[1]:,}" if isinstance(loc_data[1], int) else str(loc_data[1])

    # Line 40: Repos & Stars (Pipe at column 51, total line length 86)
    repo_length = 24 - len(contrib_str)
    justify_format(root, 'repo_data', repo_str, repo_length)
    justify_format(root, 'contrib_data', contrib_str)
    justify_format(root, 'star_data', star_str, 25)

    # Line 41: Commits & Followers (Pipe at column 51, total line length 86)
    justify_format(root, 'commit_data', commit_str, 38)
    justify_format(root, 'follower_data', follower_str, 21)

    # Line 42: Lines of Code on GitHub (Total line length 86)
    loc_right_side = f" ( {loc_add_str}++, {loc_del_str}-- )"
    loc_length = 86 - 28 - len(loc_right_side)
    justify_format(root, 'loc_data', loc_main_str, loc_length)
    justify_format(root, 'loc_add', loc_add_str)
    justify_format(root, 'loc_del', loc_del_str)
    find_and_replace(root, 'loc_del_dots', '')

    etree.ElementTree(root).write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    if length > 0:
        just_len = max(0, length - len(new_text))
        if just_len <= 2:
            dot_map = {0: '', 1: ' ', 2: '. '}
            dot_string = dot_map[just_len]
        else:
            dot_string = ' ' + ('.' * just_len) + ' '
        find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'r') as f:
        data = f.readlines()
    data = data[comment_size:]
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!) {
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    query_count('follower_getter')
    query = '''
    query($login: String!) {
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    variables = {'login': username}
    request = simple_request(follower_getter.__name__, query, variables)
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


if __name__ == '__main__':
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    for index in range(len(total_loc) - 1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', commit_data, star_data, repo_data, contrib_data, follower_data, total_loc)
    svg_overwrite('light_mode.svg', commit_data, star_data, repo_data, contrib_data, follower_data, total_loc)