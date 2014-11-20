from dock.util import split_repo_img_name_tag, join_repo_img_name_tag, get_baseimage_from_dockerfile, \
    join_repo_img_name, join_img_name_tag


TEST_DATA = [
    ("repository.com/image-name", ("repository.com", "image-name", "")),
    ("repository.com/prefix/image-name:1", ("repository.com", "prefix/image-name", "1")),
    ("image-name", ("", "image-name", "")),
    ("registry:5000/image-name:latest", ("registry:5000", "image-name", 'latest')),
    ("fedora:20", ("", "fedora", "20")),
]


TEST_DATA_IMG_TAG = [
    ("image-name", ("image-name", "")),
    ("prefix/image-name:1", ("prefix/image-name", "1")),
    ("fedora:20", ("fedora", "20")),
]


TEST_DATA_REG_IMG = [
    ("repository.com/image-name", ("repository.com", "image-name")),
    ("repository.com/prefix/image-name", ("repository.com", "prefix/image-name")),
    ("image-name", ("", "image-name")),
    ("registry:5000/image-name", ("registry:5000", "image-name")),
]


def test_split_image_repo_name():
    global TEST_DATA
    for chain, chunks in TEST_DATA:
        result = split_repo_img_name_tag(chain)
        assert result == chunks


def test_join_repo_img_name_tag():
    global TEST_DATA
    for chain, chunks in TEST_DATA:
        result = join_repo_img_name_tag(*chunks)
        assert result == chain


def test_join_reg_img():
    global TEST_DATA_REG_IMG
    for chain, chunks in TEST_DATA_REG_IMG:
        result = join_repo_img_name(*chunks)
        assert result == chain


def test_join_img_tag():
    global TEST_DATA_IMG_TAG
    for chain, chunks in TEST_DATA_IMG_TAG:
        result = join_img_name_tag(*chunks)
        assert result == chain


def test_get_baseimg_from_df():
    assert 'fedora:latest' == get_baseimage_from_dockerfile('https://github.com/TomasTomecek/docker-hello-world.git')
