from setuptools import setup, find_packages

with open('requirements.txt') as f:
    requirements = []
    for line in f:
        if not line.startswith('-f'):
            requirements.append(line)


setup(
    name = 'emergent-selfish',
    version = '0.0.1',
    url = 'https://github.com/mnoukhov/emergent-selfish',
    author = 'Michael Noukhovitch',
    author_email = 'mnoukhov@gmail.com',
    packages = find_packages(),
    install_requires=requirements,
)