import logging
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
from torch.utils.data import TensorDataset

import time
from utils import *
import copy

device = 'cuda' if torch.cuda.is_available() else 'cpu'

class Client(object):
    """Simulated federated learning client."""

    def __init__(self, client_id):
        self.client_id = client_id

    def __repr__(self):
        return 'Client #{}\n'.format(self.client_id) 
    
    # Server interactions
    def download(self, argv): #For possible future works 
        # Download from the server.
        try:
            return argv.copy()
        except:
            return argv

    def upload(self, argv): #For possible future works 
        # Upload to the server
        try:
            return argv.copy()
        except:
            return argv
            

    def set_data(self, data, config):
        """Set the client's DataLoader with its own data points."""
        if config['experiment'] == 'QM9':
            from torch_geometric.loader import DataLoader as PyGDataLoader
            DataLoader = PyGDataLoader
        else:
            from torch.utils.data import DataLoader as TorchDataLoader
            DataLoader = TorchDataLoader
        batch_size = config['hyperparameters']['local_training']['batch_size']
        self.dataloader = DataLoader(data, batch_size=batch_size, shuffle=True, drop_last=True)


        
    def configure(self, config):
        pass
        # To be implemented


    def test(self):
        # Perform local model testing - never used local testing
        raise NotImplementedError


    def get_optimizer(self, config, model):
        optim = config['hyperparameters']['local_training']['optimizer']
        lr = config['hyperparameters']['local_training']['local_lr']
        momentum = config['hyperparameters']['local_training']['local_momentum']
        model_params = []
        for task, m in model.items():
            model_params += list(m.parameters())
            
        if 'RMSprop' == optim:
            optimizer = torch.optim.RMSprop(model_params, lr=lr, momentum=momentum)
        elif 'Adam' == optim:
            optimizer = torch.optim.Adam(model_params, lr=lr)
        elif 'SGD' == optim:
            optimizer = torch.optim.SGD(model_params, lr=lr, momentum=momentum)
        return optimizer

    def local_train(self, config, global_model, experiment_module, tasks, **kwargs):
        model_device = config['model_device']
        boost_w_gpu = True if device == 'cuda' and model_device != 'cuda' else False
        return_device = 'cuda' if (model_device == 'cuda' or boost_w_gpu) else 'cpu'

                
        optimizer = self.get_optimizer(config, global_model)
        loss_fn = experiment_module.get_loss()

        if config['algorithm'] in ['fsmgda']:    
            updates = {t: {'rep': None, t: None} for t in tasks}
            for temp in updates.keys():
                updates[temp]['rep'] = None
            
            for task in tasks:
                optimizer = self.get_optimizer(config, global_model)
                initial_model = model_to_dict(global_model['rep'])
                initial_task_model = model_to_dict(global_model[task])
    
                local_update_counter = 0
                local_updates_finished_flag = False
                while not local_updates_finished_flag:
                    for batch in self.dataloader:
                        optimizer.zero_grad()
                        if local_update_counter == config['hyperparameters']['local_training']['nb_of_local_rounds']:
                            local_updates_finished_flag = True
                            break
                        
                        images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                        labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]

                        rep, _ = global_model['rep'](images, None)
                        out, _ = global_model[task](rep, None)
                        loss = loss_fn[task](out, labels)
                        loss.backward()

                        # Normalize gradients if required
                        if config['algorithm_args'][config['algorithm']]['normalize_local_iters']:
                            total_norm = 0.0
                            for name, param in global_model['rep'].named_parameters():
                                if param.grad is not None:
                                    total_norm += param.grad.data.norm(2).item() ** 2
                            for name, param in global_model[task].named_parameters():
                                if param.grad is not None:
                                    total_norm += param.grad.data.norm(2).item() ** 2
                            total_norm = total_norm ** 0.5
                            
                            # Normalize gradients
                            for name, param in global_model['rep'].named_parameters():
                                if param.grad is not None:
                                    param.grad.data.div_(total_norm)
                            for name, param in global_model[task].named_parameters():
                                if param.grad is not None:
                                    param.grad.data.div_(total_norm)
                        
                        optimizer.step()
                        local_update_counter += 1

                with torch.no_grad():
                    final_model = model_to_dict(global_model['rep'])
                    final_task_model = model_to_dict(global_model[task])
                    [reset_gradients(m) for m in [global_model['rep'], global_model[task]]]
        
                    
                    updates[task]['rep'] = {name: (final_model[name] - initial_model[name]).to(return_device) for name in final_model}
                    updates[task][task] = {name: (final_task_model[name] - initial_task_model[name]).to(return_device) for name in final_task_model}
                    
                    # Reset global model to initial state before starting next task training
                    dict_to_model(global_model['rep'], initial_model)
                    dict_to_model(global_model[task], initial_task_model)
                    
            function_return = updates    
                
        elif config['algorithm'] == 'fedcmoo':
            current_weight = kwargs['current_weight']
            if kwargs['first_local_round'] == True:
                self.initial_round_gradients = {task: {'rep': None, task: None} for task in tasks}
                G = []                
                for task in tasks:
                    initial_model = model_to_dict(global_model['rep'])
                    initial_task_model = model_to_dict(global_model[task])
                    batch = next(iter(self.dataloader))
                    images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                    labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]
                    optimizer.zero_grad()
                    rep, _ = global_model['rep'](images, None)
                    out, _ = global_model[task](rep, None)
                    loss = loss_fn[task](out, labels)
                    loss.backward()
                    
                    # Collect self.initial_round_gradients - here we ignore changes in any moving type normalization e.g. group norm
                    # To include them we would need to calculate difference between final and initial model using state_dict
                    self.grad_saving_device = {True: 'cuda', False:model_device}[boost_w_gpu and kwargs['save_to_gpu']]
                    
                    # self.grad_saving_device is generally model_device. I added an extra gpu utilization if gpu is not big enough to keep all
                    # clients' grads but we want to use at its max memory so that we keep as many grad in gpu as possible for faster training
                    with torch.no_grad():
                        self.initial_round_gradients[task]['rep'] = {name: param.grad.clone().to(self.grad_saving_device) for name, param in global_model['rep'].named_parameters()}
                        self.initial_round_gradients[task][task] = {name: param.grad.clone().to(self.grad_saving_device) for name, param in global_model[task].named_parameters()}

                # Normalize initial_round_gradients if required
                if config['algorithm_args'][config['algorithm']]['normalize_updates']:
                    for task in tasks:
                        # Compute L2 norm
                        total_norm = 0.0
                        for grad in self.initial_round_gradients[task]['rep'].values():
                            total_norm += grad.norm(2).item() ** 2
                        if config['algorithm_args'][config['algorithm']]['count_decoders']:
                            for grad in self.initial_round_gradients[task][task].values():
                                total_norm += grad.norm(2).item() ** 2
                        total_norm = total_norm ** 0.5
                        
                        # Normalize gradients
                        for name in self.initial_round_gradients[task]['rep']:
                            self.initial_round_gradients[task]['rep'][name].div_(total_norm)
                        for name in self.initial_round_gradients[task][task]:
                            self.initial_round_gradients[task][task][name].div_(total_norm)

                with torch.no_grad(): # This is faster if there is enough space on gpu
                    G_T_G = torch.zeros((len(tasks), len(tasks)), dtype=torch.float32).cpu()
                    G = []
                    for task in tasks:
                        # Form the big vector v_j for task j
                        v_j = []
                        for name in self.initial_round_gradients[task]['rep']:
                            v_j.append(self.initial_round_gradients[task]['rep'][name].view(-1).clone().cpu())
                        if config['algorithm_args'][config['algorithm']]['count_decoders']:
                            for t in tasks:
                                if t == task:
                                    for name in self.initial_round_gradients[task][task]:
                                        v_j.append(self.initial_round_gradients[task][task][name].view(-1).clone().cpu())
                                else:
                                    v_j.append(torch.zeros_like(list(global_model[t].parameters())[0]).view(-1).clone().cpu())
                        G.append(torch.cat(v_j).cpu())
                    G = torch.cat([temp.reshape(1,-1) for temp in G]).T.cpu()

                function_return = (G, None)
            else:
                initial_model = model_to_dict(global_model['rep'])
                initial_task_model = {task: model_to_dict(global_model[task]) for task in tasks}
                for m in global_model:
                    global_model[m].to(self.grad_saving_device)
                    
                for task in tasks:
                    for name, param in global_model['rep'].named_parameters():
                        if param.grad is None:
                            param.grad = self.initial_round_gradients[task]['rep'][name] * current_weight[task]
                        else:
                            param.grad += self.initial_round_gradients[task]['rep'][name] * current_weight[task]
                    for name, param in global_model[task].named_parameters():
                        temp = current_weight[task] if config['algorithm_args'][config['algorithm']]['scale_decoders'] else 1
                        if param.grad is None:
                            param.grad = self.initial_round_gradients[task][task][name] * temp
                        else:
                            param.grad += self.initial_round_gradients[task][task][name] * temp

                for m in global_model:
                    global_model[m].to(device)

                optimizer.step()

                
                # #### myDel(self.initial_round_gradients)
                if self.grad_saving_device == 'cuda':
                    myAttrDel(self, 'initial_round_gradients') 

                
                [reset_gradients(m) for m in [global_model['rep']]+ [global_model[task] for task in tasks] ]
                # myDel()

                # Continue with remaining local rounds

                local_updates_finished_flag, local_update_counter = False, 0
                
                # optimized code (summing directly losses) for no normalization and decoder scaling
                if not config['algorithm_args'][config['algorithm']]['normalize_updates'] and config['algorithm_args'][config['algorithm']]['scale_decoders']: 
                    while not local_updates_finished_flag:
                        for batch in self.dataloader:
                            weighted_loss = 0.0
                            optimizer.zero_grad()
                            if local_update_counter == config['hyperparameters']['local_training']['nb_of_local_rounds']-1:
                                local_updates_finished_flag = True
                                break

                            # Compute the total weighted loss by summing over all task losses
                            images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                            rep, _ = global_model['rep'](images, None)
                            for task in tasks:
                                labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]
                                out, _ = global_model[task](rep, None)
                                loss = loss_fn[task](out, labels)
                                weighted_loss += current_weight[task] * loss
                            # Backpropagate the combined weighted loss
                            weighted_loss.backward()
                            # Apply gradients and optimize
                            optimizer.step()
                            local_update_counter += 1

                else:
                    while not local_updates_finished_flag:
                        for batch in self.dataloader:
                            weighted_loss = 0.0
                            optimizer.zero_grad()
                            if local_update_counter == config['hyperparameters']['local_training']['nb_of_local_rounds']-1:
                                local_updates_finished_flag = True
                                break
                            
                            task_gradients = {task: {'rep': [], 'task': []} for task in tasks}
                            if device == 'cuda':    
                                torch.cuda.empty_cache()
                            
                            for task in tasks:
                                images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                                labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]
                                rep, _ = global_model['rep'](images, None)
                                out, _ = global_model[task](rep, None)
                                loss = loss_fn[task](out, labels)
                                weighted_loss += current_weight[task] * loss
                                
                                # Zero gradients for this task
                                optimizer.zero_grad()
                                
                                # Compute gradients for the task
                                loss.backward(retain_graph=True)
                                
                                # Store gradients for 'rep' and task model using state_dict
                                for name, param in global_model['rep'].state_dict(keep_vars=True).items():
                                    if param.grad is not None:
                                        # task_gradients[task]['rep'][name] = param.grad.data.clone()
                                        task_gradients[task]['rep'].append(param.grad.data.clone())
                                for name, param in global_model[task].state_dict(keep_vars=True).items():
                                    if param.grad is not None:
                                        task_gradients[task]['task'].append(param.grad.data.clone())
    
                            # Reset gradients after saving them
                            optimizer.zero_grad()
                            [reset_gradients(global_model[t]) for t in global_model]
                            
                            # Normalize gradients if required
                            if config['algorithm_args'][config['algorithm']]['normalize_updates']:
                                for task in tasks:
                                    # Compute L2 norm
                                    total_norm = 0.0
                                    for grad in task_gradients[task]['rep']:
                                        total_norm += grad.norm(2).item() ** 2
                                    if config['algorithm_args'][config['algorithm']]['count_decoders']:
                                        for grad in task_gradients[task]['task']:
                                            total_norm += grad.norm(2).item() ** 2
                                    total_norm = total_norm ** 0.5
                                    # Normalize gradients
                                    for grad in task_gradients[task]['rep']:
                                        grad.div_(total_norm)
                                    for grad in task_gradients[task]['task']:
                                        grad.div_(total_norm)
                    
                            # Apply weighted gradients
                            for task in tasks:
                                for param, grad in zip(global_model['rep'].parameters(), task_gradients[task]['rep']):
                                    if param.grad is None:
                                        param.grad = grad * current_weight[task]
                                    else:
                                        param.grad += grad * current_weight[task]
                                temp = current_weight[task] if config['algorithm_args'][config['algorithm']]['scale_decoders'] else 1
                                for param, grad in zip(global_model[task].parameters(), task_gradients[task]['task']):
                                    if param.grad is None:
                                        param.grad = grad * temp
                                    else:
                                        param.grad += grad * temp                       
                            optimizer.step()
                            local_update_counter += 1

                with torch.no_grad():
                    final_model = model_to_dict(global_model['rep'])
                    final_task_model = {task: model_to_dict(global_model[task]) for task in tasks}
                    [reset_gradients(global_model[t]) for t in global_model]

                function_return = {'rep': {name: (final_model[name] - initial_model[name]).to({True:device, False:model_device}[boost_w_gpu]) for name in final_model}, **{task: {name: (final_task_model[task][name] - initial_task_model[task][name]).to({True:device, False:model_device}[boost_w_gpu]) for name in final_task_model[task]}for task in tasks}}

        
        if config['algorithm'] == 'fedcmoo_pref':
            current_weight = kwargs['current_weight']
            if kwargs['first_local_round'] == True:
                self.initial_round_gradients = {task: {'rep': None, task: None} for task in tasks}
                G = []                
                for task in tasks:
                    initial_model = model_to_dict(global_model['rep'])
                    initial_task_model = model_to_dict(global_model[task])
                    batch = next(iter(self.dataloader))
                    images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                    labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]
                    optimizer.zero_grad()
                    rep, _ = global_model['rep'](images, None)
                    out, _ = global_model[task](rep, None)
                    loss = loss_fn[task](out, labels)
                    loss.backward()
                    # Collect self.initial_round_gradients - here we ignore changes in any moving type normalization e.g. group norm
                    # To include them we would need to calculate difference between final and initial model using state_dict
                    self.grad_saving_device = {True: 'cuda', False:model_device}[boost_w_gpu and kwargs['save_to_gpu']]
                    
                    # self.grad_saving_device is generally model_device. I added an extra gpu utilization if gpu is not big enough to keep all
                    # clients' grads but we want to use at its max memory so that we keep as many grad in gpu as possible for faster training
                    with torch.no_grad():
                        self.initial_round_gradients[task]['rep'] = {name: param.grad.clone().to(self.grad_saving_device) for name, param in global_model['rep'].named_parameters()}
                        self.initial_round_gradients[task][task] = {name: param.grad.clone().to(self.grad_saving_device) for name, param in global_model[task].named_parameters()}
                # Normalize initial_round_gradients if required
                if config['algorithm_args'][config['algorithm']]['normalize_updates']:
                    for task in tasks:
                        # Compute L2 norm
                        total_norm = 0.0
                        for grad in self.initial_round_gradients[task]['rep'].values():
                            total_norm += grad.norm(2).item() ** 2
                        if config['algorithm_args'][config['algorithm']]['count_decoders']:
                            for grad in self.initial_round_gradients[task][task].values():
                                total_norm += grad.norm(2).item() ** 2
                        total_norm = total_norm ** 0.5
                        
                        # Normalize gradients
                        for name in self.initial_round_gradients[task]['rep']:
                            self.initial_round_gradients[task]['rep'][name].div_(total_norm)
                        for name in self.initial_round_gradients[task][task]:
                            self.initial_round_gradients[task][task][name].div_(total_norm)
                with torch.no_grad(): # This is faster if there is enough space on gpu
                    G_T_G = torch.zeros((len(tasks), len(tasks)), dtype=torch.float32).cpu()
                    G = []
                    for task in tasks:
                        # Form the big vector v_j for task j
                        v_j = []
                        for name in self.initial_round_gradients[task]['rep']:
                            v_j.append(self.initial_round_gradients[task]['rep'][name].view(-1).clone().cpu())
                        if config['algorithm_args'][config['algorithm']]['count_decoders']:
                            for t in tasks:
                                if t == task:
                                    for name in self.initial_round_gradients[task][task]:
                                        v_j.append(self.initial_round_gradients[task][task][name].view(-1).clone().cpu())
                                else:
                                    v_j.append(torch.zeros_like(list(global_model[t].parameters())[0]).view(-1).clone().cpu())
                        G.append(torch.cat(v_j).cpu())
                    G = torch.cat([temp.reshape(1,-1) for temp in G]).T.cpu()

                function_return = (G, None)
            else:
                initial_model = model_to_dict(global_model['rep'])
                initial_task_model = {task: model_to_dict(global_model[task]) for task in tasks}
                for m in global_model:
                    global_model[m].to(self.grad_saving_device)
                    # 
                for task in tasks:
                    for name, param in global_model['rep'].named_parameters():
                        if param.grad is None:
                            param.grad = self.initial_round_gradients[task]['rep'][name] * current_weight[task]
                        else:
                            param.grad += self.initial_round_gradients[task]['rep'][name] * current_weight[task]
                    for name, param in global_model[task].named_parameters():
                        temp = current_weight[task] if config['algorithm_args'][config['algorithm']]['scale_decoders'] else 1
                        if param.grad is None:
                            param.grad = self.initial_round_gradients[task][task][name] * temp
                        else:
                            param.grad += self.initial_round_gradients[task][task][name] * temp

                for m in global_model:
                    global_model[m].to(device)

                optimizer.step()

                if self.grad_saving_device == 'cuda':
                    myAttrDel(self, 'initial_round_gradients') 

                [reset_gradients(m) for m in [global_model['rep']]+ [global_model[task] for task in tasks] ]

                # Continue with remaining local rounds

                local_updates_finished_flag, local_update_counter = False, 0

                
                # optimized code (summing directly losses) for no normalization and decoder scaling
                if not config['algorithm_args'][config['algorithm']]['normalize_updates'] and config['algorithm_args'][config['algorithm']]['scale_decoders']: 
                    while not local_updates_finished_flag:
                        for batch in self.dataloader:
                            weighted_loss = 0.0
                            optimizer.zero_grad()
                            if local_update_counter == config['hyperparameters']['local_training']['nb_of_local_rounds']-1:
                                local_updates_finished_flag = True
                                break

                            # Compute the total weighted loss by summing over all task losses
                            images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                            rep, _ = global_model['rep'](images, None)
                            for task in tasks:
                                labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]
                                out, _ = global_model[task](rep, None)
                                loss = loss_fn[task](out, labels)
                                weighted_loss += current_weight[task] * loss
                            # Backpropagate the combined weighted loss
                            weighted_loss.backward()
                            # Apply gradients and optimize
                            optimizer.step()
                            local_update_counter += 1
                else:
                    while not local_updates_finished_flag:
                        for batch in self.dataloader:
                            weighted_loss = 0.0
                            optimizer.zero_grad()
                            if local_update_counter == config['hyperparameters']['local_training']['nb_of_local_rounds']-1:
                                local_updates_finished_flag = True
                                break
                            
                            task_gradients = {task: {'rep': [], 'task': []} for task in tasks}
                            if device == 'cuda':    
                                torch.cuda.empty_cache()
                            
                            for task in tasks:
                                images = experiment_module.trainLoopPreprocess(batch[0].to(device)) # if device != config['data']['trainset_device'] else batch[0])
                                labels = batch[tasks.index(task) + 1].to(device) # if device != config['data']['trainset_device'] else batch[tasks.index(task) + 1]
                                rep, _ = global_model['rep'](images, None)
                                out, _ = global_model[task](rep, None)
                                loss = loss_fn[task](out, labels)
                                weighted_loss += current_weight[task] * loss
                                
                                # Zero gradients for this task
                                optimizer.zero_grad()
                                
                                # Compute gradients for the task
                                loss.backward(retain_graph=True)
                                
                                # Store gradients for 'rep' and task model using state_dict
                                for name, param in global_model['rep'].state_dict(keep_vars=True).items():
                                    if param.grad is not None:
                                        # task_gradients[task]['rep'][name] = param.grad.data.clone()
                                        task_gradients[task]['rep'].append(param.grad.data.clone())
                                for name, param in global_model[task].state_dict(keep_vars=True).items():
                                    if param.grad is not None:
                                        task_gradients[task]['task'].append(param.grad.data.clone())
    
                            # Reset gradients after saving them
                            optimizer.zero_grad()
                            [reset_gradients(global_model[t]) for t in global_model]
                            
                            # Normalize gradients if required
                            if config['algorithm_args'][config['algorithm']]['normalize_updates']:
                                for task in tasks:
                                    # Compute L2 norm
                                    total_norm = 0.0
                                    for grad in task_gradients[task]['rep']:
                                        total_norm += grad.norm(2).item() ** 2
                                    if config['algorithm_args'][config['algorithm']]['count_decoders']:
                                        for grad in task_gradients[task]['task']:
                                            total_norm += grad.norm(2).item() ** 2
                                    total_norm = total_norm ** 0.5
                                    # Normalize gradients
                                    for grad in task_gradients[task]['rep']:
                                        grad.div_(total_norm)
                                    for grad in task_gradients[task]['task']:
                                        grad.div_(total_norm)
                    
                            # Apply weighted gradients
                            for task in tasks:
                                for param, grad in zip(global_model['rep'].parameters(), task_gradients[task]['rep']):
                                    if param.grad is None:
                                        param.grad = grad * current_weight[task]
                                    else:
                                        param.grad += grad * current_weight[task]
                                temp = current_weight[task] if config['algorithm_args'][config['algorithm']]['scale_decoders'] else 1
                                for param, grad in zip(global_model[task].parameters(), task_gradients[task]['task']):
                                    if param.grad is None:
                                        param.grad = grad * temp
                                    else:
                                        param.grad += grad * temp                       
                            optimizer.step()
                            local_update_counter += 1

                with torch.no_grad():
                    final_model = model_to_dict(global_model['rep'])
                    final_task_model = {task: model_to_dict(global_model[task]) for task in tasks}
                    [reset_gradients(global_model[t]) for t in global_model]

                function_return = {'rep': {name: (final_model[name] - initial_model[name]).to({True:device, False:model_device}[boost_w_gpu]) for name in final_model}, **{task: {name: (final_task_model[task][name] - initial_task_model[task][name]).to({True:device, False:model_device}[boost_w_gpu]) for name in final_task_model[task]}for task in tasks}}

        return function_return
