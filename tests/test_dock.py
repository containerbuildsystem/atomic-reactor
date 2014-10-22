from dock.core import split_image_repo_name, create_image_repo_name


def test_split_image_repo_name():
    result = split_image_repo_name("repository.com/image-name")
    assert result == ["repository.com", "image-name"]
    result = split_image_repo_name("repository.com/prefix/image-name")
    assert result == ["repository.com", "prefix/image-name"]
    result = split_image_repo_name("image-name")
    assert result == ["", "image-name"]


def test_create_image_repo_name():
    result = create_image_repo_name("image-name", "repository.com")
    assert result == "repository.com/image-name"
    result = create_image_repo_name("prefix/image-name", "repository.com/")
    assert result == "repository.com/prefix/image-name"
