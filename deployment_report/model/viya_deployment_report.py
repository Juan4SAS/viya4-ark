####################################################################
# ### viya_deployment_report.py                                  ###
####################################################################
# ### Author: SAS Institute Inc.                                 ###
####################################################################
#                                                                ###
# Copyright (c) 2020, SAS Institute Inc., Cary, NC, USA.         ###
# All Rights Reserved.                                           ###
# SPDX-License-Identifier: Apache-2.0                            ###
#                                                                ###
####################################################################
import datetime
import json
import os

from subprocess import CalledProcessError
from typing import AnyStr, Dict, List, Optional, Text, Tuple

from deployment_report.model.static.viya_deployment_report_keys import ITEMS_KEY
from deployment_report.model.static.viya_deployment_report_keys import ViyaDeploymentReportKeys as Keys
from deployment_report.model.static.viya_deployment_report_ingress_controller import \
    ViyaDeploymentReportIngressController as IngressController
from deployment_report.model.utils.viya_deployment_report_utils import ViyaDeploymentReportUtils

from viya_ark_library.jinja2.sas_jinja2 import Jinja2TemplateRenderer
from viya_ark_library.k8s.sas_k8s_errors import KubectlRequestForbiddenError
from viya_ark_library.k8s.sas_k8s_objects import \
    KubernetesApiResources, KubernetesObjectJSONEncoder, KubernetesResource
from viya_ark_library.k8s.sas_kubectl_interface import KubectlInterface

# templates for string-formatted timestamp values #
_READABLE_TIMESTAMP_TMPL_ = "%A, %B %d, %Y %I:%M%p"
_FILE_TIMESTAMP_TMPL_ = "%Y-%m-%dT%H_%M_%S"

# templates for output file names #
_REPORT_DATA_FILE_NAME_TMPL_ = "viya_deployment_report_data_{}.json"
_REPORT_FILE_NAME_TMPL_ = "viya_deployment_report_{}.html"

# SAS custom API resource group id #
_SAS_API_GROUP_ID_ = "sas.com"


class ViyaDeploymentReport(object):
    """
    A ViyaDeploymentReport object represents a summary of all SAS components currently deployed on the target Kubernetes
    environment.

    A cache of Kubernetes resources are stored for access via the API.

    The gathered data can be written to disk as an HTML report and a JSON file containing the gathered data.
    """

    # default values for arguments shared between the model and command #
    DATA_FILE_ONLY_DEFAULT: bool = False
    INCLUDE_POD_LOG_SNIPS_DEFAULT: bool = False
    INCLUDE_RESOURCE_DEFINITIONS_DEFAULT: bool = False
    OUTPUT_DIRECTORY_DEFAULT: Text = "./"

    def __init__(self) -> None:
        """
        Constructor for ViyaDeploymentReport object.
        """
        self._report_data = None

    def gather_details(self, kubectl: KubectlInterface,
                       include_pod_log_snips: bool = INCLUDE_POD_LOG_SNIPS_DEFAULT) -> None:
        """
        This method executes the gathering of Kubernetes resources related to SAS components.
        Before this method is executed class fields will have a None value. This method will
        cache the Kubernetes resources as a dictionary, making the values accessible via the
        API for this class. The data will also be collated into relevant values used to populate
        the HTML report, which can be generated by calling the write_report() method.

        :param kubectl: The configured KubectlInterface object used to communicate with the desired Kubernetes cluster.
        :param include_pod_log_snips: If True, a snippet of the most recent log messages will be
                                      captured as part of the Pod resource. This option will cause
                                      the execution of this method to take significantly longer.
        """
        #######################################################################
        # Initialize class fields                                             #
        #######################################################################

        self._report_data: Dict = dict()

        #######################################################################
        # Initialize method fields                                            #
        #######################################################################

        # map Key classes to shorter var names for better readability
        k8s_kinds: KubernetesResource.Kinds = KubernetesResource.Kinds()

        #######################################################################
        # Gather details about Kubernetes environment where SAS is deployed   #
        #######################################################################

        # mark when these details were gathered #
        gathered: Text = datetime.datetime.now().strftime(_READABLE_TIMESTAMP_TMPL_)

        # gather Kubernetes API resources #
        api_resources: KubernetesApiResources = kubectl.api_resources()
        api_resources_dict: Dict = api_resources.as_dict()

        # make a list of any Custom Resource Definitions that are provided by SAS #
        sas_custom_resources: Dict = dict()
        for kind, details in api_resources_dict.items():
            if _SAS_API_GROUP_ID_ in api_resources.get_api_group(kind):
                sas_custom_resources[kind] = details

        # create dictionary to store gathered resources #
        gathered_resources: Dict = dict()

        # start by gathering details about Nodes, if available #
        # this information can be reported even if Pods are not listable #
        try:
            ViyaDeploymentReportUtils.gather_resource_details(kubectl, gathered_resources, api_resources,
                                                              k8s_kinds.NODE)
        except CalledProcessError:
            # the user may not be able to see non-namespaced resources like nodes, move on without raising an error #
            pass

        # gather details about Pods in the target Kubernetes cluster                                                   #
        # Pods are the smallest unit in Kubernetes and define 'ownerReferences', which can be used to gather upstream  #
        # relationships                                                                                                #
        # services, ingresses, and virtual services will be gathered separately even if Pods and their owners are      #
        # found; networking resources are not defined in 'ownerReferences'                                             #
        # if Pods cannot be listed, the report will report the details already gathered, and display a messages saying #
        # that components could not be reported because pods are not listable                                          #
        try:
            ViyaDeploymentReportUtils.gather_resource_details(kubectl, gathered_resources, api_resources, k8s_kinds.POD)
        except CalledProcessError:
            # if a CalledProcessError is raised when gathering pods, then surface an error up to stop the program #
            # without the ability to list pods, aggregating component resources won't occur #
            raise KubectlRequestForbiddenError(f"Listing pods is forbidden in namespace [{kubectl.get_namespace()}]. "
                                               "Make sure KUBECONFIG is correctly set and that the correct namespace "
                                               "is being targeted. A namespace can be given on the command line using "
                                               "the \"--namespace=\" option.")

        # define a list to hold any unavailable resources #
        unavailable_resources: List = list()
        ingress_ctlr: Optional[Text] = None

        #######################################################################
        # Start - Additional resource gathering                               #
        #######################################################################

        # make sure pods were gathered #
        # if none were found, there is no need to gather additional details #
        if gathered_resources[k8s_kinds.POD][Keys.KindDetails.COUNT] > 0:

            try:
                # since Pods were listable, gather details about networking kinds #
                for networking_kind in [k8s_kinds.SERVICE, k8s_kinds.INGRESS, k8s_kinds.ISTIO_VIRTUAL_SERVICE]:
                    ViyaDeploymentReportUtils.gather_resource_details(kubectl, gathered_resources, api_resources,
                                                                      networking_kind)

                # make sure an attempt is made to gather any SAS CDRs that were discovered    #
                # some may have already been gathered if they are upstream owners of any Pods #
                for sas_custom_kind in sas_custom_resources.keys():
                    ViyaDeploymentReportUtils.gather_resource_details(kubectl, gathered_resources, api_resources,
                                                                      sas_custom_kind)
            except CalledProcessError:
                # if any of the networking or SAS CRDs can't be listed, move since some amount of component resources #
                # have already been gathered #
                pass

            # determine the ingress controller #
            ingress_ctlr = ViyaDeploymentReportUtils.determine_ingress_controller(gathered_resources)

            # determine if any discovered resource kinds were unavailable                                            #
            # if at least one is unavailable, a message will be displayed saying that components may not be complete #
            # because all resources were not listable                                                                #
            for kind, kind_details in gathered_resources.items():
                # check if the kind is unavailable #
                if not kind_details[Keys.KindDetails.AVAILABLE]:
                    # ignore the unavailable Ingress kind if Istio is used or VirtualService kind if Nginx is used
                    if (ingress_ctlr == IngressController.ISTIO and kind != k8s_kinds.INGRESS) or \
                            (ingress_ctlr == IngressController.KUBE_NGINX and kind != k8s_kinds.ISTIO_VIRTUAL_SERVICE):

                        unavailable_resources.append(kind)

            #######################################################################
            # Define relationships between resources                              #
            #######################################################################

            # define the relationship between Service and Ingress #
            ViyaDeploymentReportUtils.define_service_to_ingress_relationships(gathered_resources[k8s_kinds.SERVICE],
                                                                              gathered_resources[k8s_kinds.INGRESS])

            # define the relationship between Service and VirtualService #
            ViyaDeploymentReportUtils.define_service_to_virtual_service_relationships(
                gathered_resources[k8s_kinds.SERVICE], gathered_resources[k8s_kinds.ISTIO_VIRTUAL_SERVICE])

            # define the relationship between Pod and Service #
            ViyaDeploymentReportUtils.define_pod_to_service_relationships(gathered_resources[k8s_kinds.POD],
                                                                          gathered_resources[k8s_kinds.SERVICE])

            # define the relationship between Node and Pod #
            ViyaDeploymentReportUtils.define_node_to_pod_relationships(gathered_resources[k8s_kinds.NODE],
                                                                       gathered_resources[k8s_kinds.POD])

            #######################################################################
            # Get metrics                                                         #
            #######################################################################

            # get Pod metrics #
            ViyaDeploymentReportUtils.get_pod_metrics(kubectl, gathered_resources[k8s_kinds.POD])

            # get Node metrics #
            ViyaDeploymentReportUtils.get_node_metrics(kubectl, gathered_resources[k8s_kinds.NODE])

            #######################################################################
            # Gather Pod logs, if requested                                       #
            #######################################################################

            # check if logs were requested #
            if include_pod_log_snips:
                # loop over all Pods to get their log snips #
                for pod_name, pod_details in gathered_resources[k8s_kinds.POD][ITEMS_KEY].items():
                    try:
                        # define the log snip extension for this Pod
                        log_snip: List = kubectl.logs(pod_name)

                        pod_details[Keys.ResourceDetails.EXT_DICT][Keys.ResourceDetails.Ext.LOG_SNIP_LIST]: List \
                            = log_snip
                    except CalledProcessError:
                        # if the logs can't be retrieved, move on without error #
                        pass

        #######################################################################
        # End - Additional resource gathering                                 #
        #######################################################################

        #######################################################################
        # Create the report data dictionary                                   #
        #######################################################################

        # add the gathered time #
        self._report_data[Keys.GATHERED]: Text = gathered

        # add any unavailable resources #
        self._report_data[Keys.UNAVAILABLE_RESOURCES_LIST]: List = unavailable_resources

        # add details about the Kubernetes cluster under the 'kubernetes' key #
        k8s_details_dict = self._report_data[Keys.KUBERNETES_DICT] = dict()
        # create a key to hold the API resources available in the cluster: dict #
        k8s_details_dict[Keys.Kubernetes.API_RESOURCES_DICT]: Dict = api_resources_dict
        # create a key to hold the API versions in the cluster: list #
        k8s_details_dict[Keys.Kubernetes.API_VERSIONS_LIST]: List = kubectl.api_versions()
        # create a key to mark the determined ingress controller for the cluster: str|None #
        k8s_details_dict[Keys.Kubernetes.INGRESS_CTRL]: Optional[Text] = ingress_ctlr
        # create a key to mark the namespace evaluated for this report: str|None #
        k8s_details_dict[Keys.Kubernetes.NAMESPACE] = kubectl.get_namespace()
        # create a key to hold the details about node in the cluster: dict #
        k8s_details_dict[Keys.Kubernetes.NODES_DICT]: Dict = gathered_resources[k8s_kinds.NODE]
        # create a key to hold the client/server versions for the cluster: dict #
        k8s_details_dict[Keys.Kubernetes.VERSIONS_DICT]: Dict = kubectl.version()
        # create a key to hold the meta information about resources discovered in the cluster: dict #
        k8s_details_dict[Keys.Kubernetes.DISCOVERED_KINDS_DICT]: Dict = dict()

        # add the availability and count of all discovered resources #
        for kind_name, kind_details in gathered_resources.items():
            # create an entry for the kind #
            k8s_details_dict[Keys.Kubernetes.DISCOVERED_KINDS_DICT][kind_name]: Dict = dict()
            # create a key to mark if the resource kind was available: bool #
            k8s_details_dict[Keys.Kubernetes.DISCOVERED_KINDS_DICT][kind_name][Keys.KindDetails.AVAILABLE]: bool = \
                kind_details[Keys.KindDetails.AVAILABLE]
            # create a key to note the total count of the resource kind: int #
            k8s_details_dict[Keys.Kubernetes.DISCOVERED_KINDS_DICT][kind_name][Keys.KindDetails.COUNT]: int = \
                kind_details[Keys.KindDetails.COUNT]
            # create a key to note whether this kind is a SAS custom resource definition: bool #
            k8s_details_dict[Keys.Kubernetes.DISCOVERED_KINDS_DICT][kind_name][Keys.KindDetails.SAS_CRD]: bool = \
                kind_name in sas_custom_resources

        # if Pods are defined, associate the resources that comprise a component #
        if gathered_resources[k8s_kinds.POD][Keys.KindDetails.COUNT] > 0:

            # create the sas and misc entries in report_data #
            sas_dict = self._report_data[Keys.SAS_COMPONENTS_DICT] = dict()
            misc_dict = self._report_data[Keys.OTHER_COMPONENTS_DICT] = dict()

            # create components by building them up from the Pod via relationship extensions #
            for pod_details in gathered_resources[k8s_kinds.POD][ITEMS_KEY].values():
                # define a dictionary to hold the aggregated component #
                component: Dict = dict()

                # aggregate all the resources related to this Pod into a component #
                ViyaDeploymentReportUtils.aggregate_component_resources(pod_details, gathered_resources, component)

                # determine if this component belongs to SAS and it's component name value #
                is_sas_component: bool = False
                component_name: Optional[Text] = None

                # iterate over all resource kinds in the component #
                for kind_details in component.values():
                    # iterate over all resources of this kind #
                    for resource_details in kind_details.values():
                        # see if this resource is a SAS resource, if so this component will be treated as a SAS #
                        # component                                                                             #
                        if is_sas_component is False and \
                                resource_details[Keys.ResourceDetails.RESOURCE_DEFINITION].is_sas_resource():
                            is_sas_component = True

                        # see if this resource is a controller and has a component name extension #
                        if component_name is None and \
                                Keys.ResourceDetails.Ext.COMPONENT_NAME in resource_details[
                                Keys.ResourceDetails.EXT_DICT]:

                            component_name = resource_details[Keys.ResourceDetails.EXT_DICT][
                                Keys.ResourceDetails.Ext.COMPONENT_NAME]

                # add the component to its appropriate dictionary
                if is_sas_component:
                    if component_name not in sas_dict:
                        # if this component is being added for the first time, create its key #
                        sas_dict[component_name]: Dict = component
                    else:
                        # otherwise, merge its kinds #
                        for kind_name, kind_details in component.items():
                            if kind_name not in sas_dict[component_name]:
                                sas_dict[component_name][kind_name]: Dict = kind_details
                            else:
                                for resource_name, resource_details in kind_details.items():
                                    sas_dict[component_name][kind_name][resource_name]: Dict = resource_details

                else:
                    misc_dict[component_name]: Dict = component

    def get_kubernetes_details(self) -> Optional[Dict]:
        """
        Returns the details gathered about the target Kubernetes cluster.

        :return: A dictionary of details about the targeted Kubernetes cluster or None if details have not been
                 gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT]
        except KeyError:
            return None

    def get_api_resources(self) -> Optional[Dict]:
        """
        Convenience method for getting the API resources from the Kubernetes cluster details.

        :return: A dictionary of details about the API resources available in the Kubernetes cluster or None if details
                 have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.API_RESOURCES_DICT]
        except KeyError:
            return None

    def get_api_versions(self) -> Optional[List]:
        """
        Convenience method for getting the API versions from the Kubernetes cluster details

        :return: A list of API versions available in the Kubernetes cluster or None if details have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.API_VERSIONS_LIST]
        except KeyError:
            return None

    def get_discovered_resources(self) -> Optional[Dict]:
        """
        Convenience method for getting the resources discovered during gathering from the Kubernetes cluster details.

        :return: A dictionary of details about the Resources discovered relating to SAS components or None if details
                 have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.DISCOVERED_KINDS_DICT]
        except KeyError:
            return None

    def get_ingress_controller(self) -> Optional[Text]:
        """
        Convenience method for getting the ingress controller from the Kubernetes cluster details.

        :return: The Kubernetes ingress controller or None if one could not be determined or details have not been
                 gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.INGRESS_CTRL]
        except KeyError:
            return None

    def get_namespace(self) -> Optional[Text]:
        """
        Convenience method for getting the namespace value from the Kubernetes cluster details.

        :return: The Kubernetes namespace targeted or None if details have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.NAMESPACE]
        except KeyError:
            return None

    def get_node_details(self) -> Optional[Dict]:
        """
        Convenience method for getting the Node details from the Kubernetes cluster details.

        :return: A dictionary of details about Nodes in the Kubernetes cluster or None if details have not been
                 gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.NODES_DICT]
        except KeyError:
            return None

    def get_kubernetes_version_details(self) -> Optional[Dict]:
        """
        Convenience method for getting the version details from the Kubernetes cluster details.

        :return: A dictionary of details about Kuberentes versions or None if details have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.KUBERNETES_DICT][Keys.Kubernetes.VERSIONS_DICT]
        except KeyError:
            return None

    def get_other_components(self) -> Optional[Dict]:
        """
        Returns the non-SAS components gathered in the target Kubernetes cluster.

        :return: A dictionary of details about non-SAS components or None if details have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.OTHER_COMPONENTS_DICT]
        except KeyError:
            return None

    def get_sas_components(self) -> Optional[Dict]:
        """
        Returns the SAS components gathered in the target Kubernetes cluster.

        :return: A dictionary of details about SAS components or None if details have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.SAS_COMPONENTS_DICT]
        except KeyError:
            return None

    def get_sas_component(self, component_name: Text) -> Optional[Dict]:
        """
        Convenience method for getting the details about a specific SAS component.

        :param component_name: The name of the SAS component whose details will be retrieved.
        :return: A dictionary of details about the requested SAS component or None if the component does not exist or
                 details have not been gathered.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.SAS_COMPONENTS_DICT][component_name]
        except KeyError:
            return None

    def get_sas_component_resources(self, component_name: Text, resource_kind: Text) -> Optional[Dict]:
        """
        Convenience method for getting the details about specific resources for a specific SAS component.

        :param component_name: The name of the SAS component whose details will be retrieved.
        :param resource_kind: The kind value of the resources to retrieve.
        :return: A dictionary of details about all resources of the requested kind from the requested SAS component or
                 None if details have not been gathered, the component doesn't exist or the component doesn't define the
                 requested resource_kind.
        """
        if self._report_data is None:
            return None

        try:
            return self._report_data[Keys.SAS_COMPONENTS_DICT][component_name][resource_kind]
        except KeyError:
            return None

    def write_report(self, output_directory: Text = OUTPUT_DIRECTORY_DEFAULT,
                     data_file_only: bool = DATA_FILE_ONLY_DEFAULT,
                     include_resource_definitions: bool = INCLUDE_RESOURCE_DEFINITIONS_DEFAULT,
                     file_timestamp: Optional[Text] = None) -> Tuple[Optional[AnyStr], Optional[AnyStr]]:
        """
        Writes the report data to a file as a JSON string.

        :param output_directory: The directory where the file should be written.
        :param data_file_only: If True, only the report data JSON file is created.
        :param file_timestamp: A timestamp to use for the file. If one isn't provided, a new one will be created.
        :param include_resource_definitions: If true, the full JSON resource definition will be included for each
                                             object in the report. Otherwise, the definition will be omitted.
        """
        if self._report_data is None:
            return None, None

        # get a timestamp if one isn't given #
        if file_timestamp is None:
            file_timestamp = datetime.datetime.now().strftime(_FILE_TIMESTAMP_TMPL_)

        # make sure path is valid #
        if output_directory != "" and not output_directory.endswith(os.sep):
            output_directory = output_directory + os.sep

        # convert the data to a JSON string #
        data_json = json.dumps(self._report_data, cls=KubernetesObjectJSONEncoder, indent=4, sort_keys=True)

        # write the report data #
        data_file_path: Text = output_directory + _REPORT_DATA_FILE_NAME_TMPL_.format(file_timestamp)
        with open(data_file_path, "w+") as data_file:
            data_file.write(data_json)

        # write the html file, if requested #
        html_file_path: Optional[Text] = None
        if not data_file_only:
            html_file_path = output_directory + _REPORT_FILE_NAME_TMPL_.format(file_timestamp)
            templates_dir = os.path.dirname(os.path.realpath(__file__)) + os.sep + ".." + os.sep + "templates" + os.sep
            template_renderer = Jinja2TemplateRenderer(templates_dir=templates_dir)
            html_file_path = template_renderer.as_html("viya_deployment_report.html.j2", html_file_path,
                                                       trim_blocks=True, lstrip_blocks=True,
                                                       report_data=json.loads(data_json),
                                                       include_definitions=include_resource_definitions)

        return os.path.abspath(data_file_path), html_file_path
