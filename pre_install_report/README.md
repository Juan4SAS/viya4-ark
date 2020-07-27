# Pre-Installation Check of SAS Viya System Requirements

This tool compares your Kubernetes environment to the SAS Viya system requirements. It evaluates a number of items, such as memory, CPU cores, software versions, and permissions. The output is a web-viewable, HTML report with the results. 

SAS recommends running the tool and resolving any reported issues before beginning a SAS Viya deployment in a Kubernetes cluster. 

## Prerequisites 
- The tool should be run on a machine from which the Kubernetes command-line interface, `kubectl`, can access the Kubernetes cluster. 
- The tool requires Python 3.6 or higher.  

### Required Python Packages
SAS Viya ARKcd tools require third-party packages be installed before use. All required packages can be installed using the provided `requirements.txt`:

```commandline
$ python3 -m pip install -r requirements.txt
```

Download the latest version of this tool and update required packages with every new software order.

## Usage

**Note:** You must set your `KUBECONFIG` environment variable. `KUBECONFIG` must have administrator rights in the namespace where you intend to deploy your SAS Viya software.

After obtaining the latest version of this tool, cd to `<tool-download-dir>/viya-arkcd`. 

The following command provides usage details:

```
python viya-arkcd.py pre-install-report -h
```

## Report Output

The tool generates the pre-install check report,`viya_pre_install_report_<timestamp>.html`. The report is in a web-viewable, HTML format.

