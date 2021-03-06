from pylons.i18n import _
from pylons import config
from paste.deploy.converters import asbool

from ckan.lib.base import (c, render, model, request, h, g,
    response, abort, redirect)
from ckan.controllers.package import PackageController
from ckan.logic import ValidationError, NotAuthorized

from ckanext.recombinant.errors import RecombinantException, BadExcelData
from ckanext.recombinant.read_excel import read_excel, get_records
from ckanext.recombinant.write_excel import excel_template
from ckanext.recombinant.tables import get_chromo, get_geno
from ckanext.recombinant.helpers import recombinant_primary_key_fields

from cStringIO import StringIO

import ckanapi


class UploadController(PackageController):
    """
    Controller for downloading Excel templates and
    uploading packages via Excel .xls files
    """

    def upload(self, id):
        package_type = self._get_package_type(id)
        geno = get_geno(package_type)
        lc = ckanapi.LocalCKAN(username=c.user)
        dataset = lc.action.package_show(id=id)
        try:
            if request.POST['xls_update'] == '':
                raise BadExcelData('You must provide a valid file')

            _process_upload_file(
                lc,
                dataset,
                request.POST['xls_update'].file,
                geno)

            h.flash_success(_(
                "Your file was successfully uploaded into the central system."
                ))

            redirect(h.url_for(controller='package', action='read', id=id))
        except BadExcelData, e:
            x_vars = {'errors': [e.message], 'action': 'edit'}
            c.pkg_dict = dataset
            return render(self._edit_template(package_type), extra_vars=x_vars)

    def delete_record(self, id, resource_id):
        lc = ckanapi.LocalCKAN(username=c.user)
        filters = {}
        res = lc.action.resource_show(id=resource_id)
        for f in recombinant_primary_key_fields(res['name']):
           filters[f['datastore_id']] = request.POST.get(f['datastore_id'], '')

        result = lc.action.datastore_search(
            resource_id=resource_id,
            filters=filters,
            rows=2)  # only need two to know if there are multiple matches
        records = result['records']

        x_vars = {'filters': filters, 'action': 'edit'}
        if not records:
            x_vars['delete_errors'] = [_('No matching records found')]
        elif len(records) > 1:
            x_vars['delete_errors'] = [_('Multiple matching records found')]

        if 'delete_errors' in x_vars:
            c.pkg_dict = dataset = lc.action.package_show(id=id)
            return render('recombinant/resource_edit.html',
                extra_vars=dict(x_vars, dataset=dataset, resource=res))

        # XXX: can't avoid the race here with the existing datastore API.
        # datastore_delete doesn't support _id filters
        lc.action.datastore_delete(
            resource_id=resource_id,
            filters=filters,
            )
        h.flash_success(_(
            "Record deleted."
            ))

        redirect(h.url_for(
            controller='ckanext.recombinant.controller:PreviewController',
            action='preview_table', id=id, resource_id=resource_id))


    def template(self, id):
        lc = ckanapi.LocalCKAN(username=c.user)
        dataset = lc.action.package_show(id=id)
        org = lc.action.organization_show(
            id=dataset['owner_org'],
            include_datasets=False)

        book = excel_template(dataset['type'], org)
        blob = StringIO()
        book.save(blob)
        response.headers['Content-Type'] = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        response.headers['Content-Disposition'] = (
            'inline; filename="{0}.{1}.xlsx"'.format(
                dataset['organization']['name'],
                dataset['type']))
        return blob.getvalue()


def _process_upload_file(lc, dataset, upload_file, geno):
    """
    Use lc.action.datastore_upsert to load data from upload_file

    raises BadExcelData on errors.
    """
    owner_org = dataset['organization']['name']

    expected_sheet_names = dict(
        (resource['name'], resource['id'])
        for resource in dataset['resources'])

    upload_data = read_excel(upload_file)
    while True:
        try:
            sheet_name, org_name, column_names, rows = next(upload_data)
        except StopIteration:
            return
        except:
            # XXX bare except because this can fail in all sorts of ways
            if asbool(config.get('debug', False)):
                # on debug we want the real error
                raise
            raise BadExcelData(
                _("The server encountered a problem processing the file "
                "uploaded. Please try copying your data into the latest "
                "version of the template and uploading again. If this "
                "problem continues, send your Excel file to "
                "open-ouvert@tbs-sct.gc.ca so we may investigate."))

        if sheet_name not in expected_sheet_names:
            raise BadExcelData(_('Invalid file for this data type. ' +
                'Sheet must be labeled "{0}", ' +
                'but you supplied a sheet labeled "{1}"').format(
                    '"/"'.join(sorted(expected_sheet_names)),
                    sheet_name))

        if org_name != owner_org:
            raise BadExcelData(_(
                'Invalid sheet for this organization. ' +
                'Sheet must be labeled for {0}, ' +
                'but you supplied a sheet for {1}').format(
                    owner_org, org_name))

        # custom styles or other errors cause columns to be read
        # that actually have no data. strip them here to avoid error below
        while column_names[-1] is None:
            column_names.pop()

        chromo = get_chromo(sheet_name)
        expected_columns = [f['datastore_id'] for f in chromo['fields']]
        if column_names != expected_columns:
            raise BadExcelData(
                _("This template is out of date. "
                "Please try copying your data into the latest "
                "version of the template and uploading again. If this "
                "problem continues, send your Excel file to "
                "open-ouvert@tbs-sct.gc.ca so we may investigate."))

        records = get_records(rows, chromo['fields'])
        method = 'upsert' if chromo.get('datastore_primary_key') else 'insert'
        lc.action.datastore_upsert(
            method=method,
            resource_id=expected_sheet_names[sheet_name],
            records=records)


class PreviewController(PackageController):

    def preview_table(self, id, resource_id):
        lc = ckanapi.LocalCKAN(username=c.user)
        dataset = lc.action.package_show(id=id)
        try:
            get_geno(dataset['type'])
        except RecombinantException:
            abort(404, _('Recombinant dataset_type not found'))

        for r in dataset['resources']:
            if r['id'] == resource_id:
                break
        else:
            abort(404, _('Resource not found'))

        return render('recombinant/resource_edit.html', extra_vars={
            'dataset': dataset,
            'resource': r,
            })
